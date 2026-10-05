from collections import deque
from datetime import datetime, timedelta

from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


def _require(data, fields):
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            raise ValidationError("missing required field: " + field)


def _ensure_role(actor, allowed):
    if "*" not in allowed and actor.role not in allowed:
        raise PermissionDenied("role %s is not allowed here" % actor.role)


def _all(lookup, kind):
    return lookup(kind, "*", None) or [] if lookup else []


def _find_one(lookup, kind, field, value):
    rows = lookup(kind, field, value) or [] if lookup else []
    return rows[0] if rows else None


def _number(value, field):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be numeric")


def _validate_worker(data):
    if len(str(data.get("name", "")).strip()) < 2:
        raise ValidationError("worker name is too short")


def _validate_sensor(data):
    gas = _number(data.get("gas_ppm"), "gas_ppm")
    threshold = _number(data.get("threshold_ppm"), "threshold_ppm")
    if gas < 0 or threshold <= 0:
        raise ValidationError("gas readings and thresholds must be positive")
    data["severity"] = "alarm" if gas >= threshold * 1.5 else "warning" if gas >= threshold else "normal"


def _validate_capacity(data, field):
    if _number(data.get(field), field) <= 0:
        raise ValidationError(field + " must be positive")


def _validate_passage(data):
    if _number(data.get("width_m"), "width_m") <= 0:
        raise ValidationError("width_m must be positive")
    if data.get("from_location") == data.get("to_location"):
        raise ValidationError("passage endpoints must differ")


def _validate_incident(data):
    if data.get("severity") not in ("low", "medium", "high", "critical"):
        raise ValidationError("invalid incident severity")


def _validate_task(data, lookup):
    incident = _find_one(lookup, "incident", "id", data.get("incident_id"))
    if not incident or incident["status"] in ("closed",):
        raise ValidationError("task requires an open incident")
    if data.get("task_type") not in ("evacuation", "search", "rescue", "ventilation", "medical", "repair"):
        raise ValidationError("invalid task_type")
    key = data.get("dedupe_key")
    for task in _all(lookup, "task"):
        if task["data"].get("dedupe_key") == key and task["status"] not in ("completed", "cancelled"):
            raise ConflictError("active task already exists for dedupe_key: " + str(key))


def _validate_offline(data):
    if not isinstance(data.get("payload"), dict):
        raise ValidationError("offline payload must be an object")
    try:
        datetime.fromisoformat(str(data.get("recorded_at")).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("recorded_at must be ISO-8601")


def _sensor_alarm(actor, entity, data, lookup):
    if float(entity["data"].get("gas_ppm", 0)) < float(entity["data"].get("threshold_ppm", 1)):
        raise ValidationError("alarm requires a reading at or above threshold")
    return {"acknowledged_by": actor.user_id}


def _complete_task(actor, entity, data, lookup):
    if not str(data.get("result", "")).strip():
        raise ValidationError("result is required")
    return {"completed_by": actor.user_id}


def _close_incident(actor, entity, data, lookup):
    workers = _all(lookup, "worker")
    if [w for w in workers if w["status"] in ("missing", "located")]:
        raise ConflictError("cannot close incident while workers are missing or located")
    if [w for w in workers if w["status"] == "active"]:
        raise ConflictError("cannot close incident while on-duty workers are neither sheltered nor awaiting rescue")
    active_tasks = [t for t in _all(lookup, "task") if t["status"] not in ("completed", "cancelled")]
    if active_tasks:
        raise ConflictError("cannot close incident while tasks remain active")
    if [v for v in _all(lookup, "ventilation") if v["status"] != "running"]:
        raise ConflictError("cannot close incident until ventilation is restored")
    return {"closed_by": actor.user_id}


def _build_passage_graph(passages):
    """Adjacency of locations linked by currently open passages (undirected)."""
    graph = {}
    for passage in passages:
        if passage["status"] != "open":
            continue
        frm = passage["data"].get("from_location")
        to = passage["data"].get("to_location")
        graph.setdefault(frm, []).append((to, passage["id"]))
        graph.setdefault(to, []).append((frm, passage["id"]))
    return graph


def _bfs_route(graph, start, target_by_location):
    """Shortest path from start to any target location; returns (passage_ids, refuge_id)."""
    if start in target_by_location:
        return [], target_by_location[start]
    visited = {start}
    queue = deque([(start, [])])
    while queue:
        location, path = queue.popleft()
        for neighbor, passage_id in graph.get(location, []):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            next_path = path + [passage_id]
            if neighbor in target_by_location:
                return next_path, target_by_location[neighbor]
            queue.append((neighbor, next_path))
    return None, None


def _refuge_occupancy(lookup, exclude_plan_id=None):
    """Occupied refuge slots from planned/arrived assignments of active plans."""
    occupancy = {}
    for plan in _all(lookup, "evacuation_plan"):
        if plan["status"] != "active" or plan["id"] == exclude_plan_id:
            continue
        for assignment in plan["data"].get("assignments", []):
            refuge_id = assignment.get("refuge_id")
            if refuge_id and assignment.get("status") in ("planned", "arrived"):
                occupancy[refuge_id] = occupancy.get(refuge_id, 0) + 1
    return occupancy


def compute_routes(lookup, workers, base_occupancy=None):
    """Route each worker to the nearest refuge with spare capacity.

    Workers that would exceed a refuge's rated capacity are refused
    (status unassigned); workers with no physical route are unreachable.
    """
    graph = _build_passage_graph(_all(lookup, "passage"))
    refuges = [r for r in _all(lookup, "refuge") if r["status"] != "maintenance"]
    occupancy = dict(base_occupancy or {})
    assignments = []
    for worker in workers:
        start = worker["data"].get("location_code")
        with_capacity = {}
        for refuge in refuges:
            capacity = _number(refuge["data"].get("capacity"), "capacity")
            location = refuge["data"].get("location_code")
            if occupancy.get(refuge["id"], 0) < capacity and location not in with_capacity:
                with_capacity[location] = refuge["id"]
        path, refuge_id = _bfs_route(graph, start, with_capacity)
        if refuge_id:
            occupancy[refuge_id] = occupancy.get(refuge_id, 0) + 1
            assignments.append({
                "worker_id": worker["id"],
                "refuge_id": refuge_id,
                "passage_ids": path,
                "status": "planned",
            })
            continue
        any_refuge = {}
        for refuge in refuges:
            any_refuge.setdefault(refuge["data"].get("location_code"), refuge["id"])
        _, reachable_id = _bfs_route(graph, start, any_refuge)
        if reachable_id:
            assignments.append({
                "worker_id": worker["id"],
                "refuge_id": None,
                "passage_ids": [],
                "status": "unassigned",
                "reason": "refuge_capacity_exceeded",
            })
        else:
            assignments.append({
                "worker_id": worker["id"],
                "refuge_id": None,
                "passage_ids": [],
                "status": "unreachable",
                "reason": "no_route",
            })
    return assignments


def _validate_evacuation_plan(data, lookup):
    incident = _find_one(lookup, "incident", "id", data.get("incident_id"))
    if not incident or incident["status"] == "closed":
        raise ValidationError("evacuation plan requires an open incident")
    if not [s for s in _all(lookup, "sensor") if s["status"] == "alarm"]:
        raise ValidationError("evacuation plan requires an active gas alarm")
    for plan in _all(lookup, "evacuation_plan"):
        if plan["data"].get("incident_id") == data.get("incident_id") and plan["status"] in ("draft", "active"):
            raise ConflictError("an evacuation plan already exists for incident: " + str(data.get("incident_id")))


def _submit_evacuation_plan(actor, entity, data, lookup):
    workers = [w for w in _all(lookup, "worker") if w["status"] == "active"]
    occupancy = _refuge_occupancy(lookup, exclude_plan_id=entity["id"])
    assignments = compute_routes(lookup, workers, occupancy)
    return {"assignments": assignments, "planned_by": actor.user_id}


def _worker_shelter(actor, entity, data, lookup):
    refuge_id = data.get("refuge_id")
    if not _find_one(lookup, "refuge", "id", refuge_id):
        raise ValidationError("unknown refuge: " + str(refuge_id))
    for plan in _all(lookup, "evacuation_plan"):
        if plan["status"] != "active":
            continue
        for assignment in plan["data"].get("assignments", []):
            if (
                assignment.get("worker_id") == entity["id"]
                and assignment.get("status") == "planned"
                and assignment.get("refuge_id") == refuge_id
            ):
                return {"sheltered_refuge_id": refuge_id, "plan_id": plan["id"]}
    raise ValidationError("no planned evacuation assignment for this worker at refuge: " + str(refuge_id))


class RuleEngine:
    ALIASES = {
        "workers": "worker", "sensors": "sensor", "ventilations": "ventilation",
        "passages": "passage", "refuges": "refuge", "incidents": "incident",
        "tasks": "task", "offline-records": "offline_record", "offline_records": "offline_record",
        "evacuation-plans": "evacuation_plan", "evacuation_plans": "evacuation_plan",
        "plans": "evacuation_plan",
    }
    INITIAL_STATUS = {
        "worker": "active", "sensor": "normal", "ventilation": "running",
        "passage": "open", "refuge": "available", "incident": "detected",
        "task": "proposed", "offline_record": "merged", "evacuation_plan": "draft",
    }
    TRANSITIONS = {
        "worker": {
            "mark_missing": (("active",), "missing"),
            "locate": (("missing",), "located"),
            "evacuate": (("missing", "located"), "evacuated"),
            "rescue": (("missing", "located", "awaiting_rescue"), "rescued"),
            "find_safe": (("missing",), "active"),
            "shelter": (("active",), "sheltered"),
            "mark_awaiting_rescue": (("active",), "awaiting_rescue"),
            "reroute": (("awaiting_rescue",), "active"),
            "deactivate": (("active",), "inactive"),
        },
        "sensor": {
            "raise_warning": (("normal",), "warning"),
            "raise_alarm": (("normal", "warning"), "alarm"),
            "clear": (("warning", "alarm"), "normal"),
            "mark_faulty": (("normal", "warning", "alarm"), "faulty"),
            "verify_misread": (("faulty",), "normal"),
        },
        "ventilation": {
            "degrade": (("running",), "degraded"),
            "stop": (("running", "degraded"), "stopped"),
            "restore": (("stopped", "degraded"), "running"),
        },
        "passage": {
            "restrict": (("open",), "restricted"),
            "block": (("open", "restricted"), "blocked"),
            "clear": (("blocked", "restricted"), "open"),
        },
        "refuge": {
            "occupy": (("available",), "occupied"),
            "release": (("occupied",), "available"),
            "maintain": (("available",), "maintenance"),
            "reopen": (("maintenance",), "available"),
        },
        "incident": {
            "begin_evacuation": (("detected",), "evacuating"),
            "search": (("evacuating",), "searching"),
            "stabilize": (("searching",), "stabilizing"),
            "recover": (("stabilizing",), "recovering"),
            "close": (("recovering",), "closed"),
            "reopen": (("closed",), "detected"),
        },
        "task": {
            "assign": (("proposed",), "assigned"),
            "accept": (("assigned",), "in_progress"),
            "complete": (("in_progress",), "completed"),
            "cancel": (("proposed", "assigned", "in_progress"), "cancelled"),
        },
        "evacuation_plan": {
            "submit": (("draft",), "active"),
            "cancel": (("draft", "active"), "cancelled"),
        },
    }
    CREATE_REQUIRED = {
        "worker": ("name", "location_code", "team"),
        "sensor": ("location_code", "gas_ppm", "threshold_ppm"),
        "ventilation": ("name", "area_code", "capacity"),
        "passage": ("from_location", "to_location", "width_m"),
        "refuge": ("location_code", "capacity"),
        "incident": ("area_code", "severity", "summary"),
        "task": ("incident_id", "task_type", "target", "dedupe_key"),
        "offline_record": ("source_id", "record_id", "recorded_at", "payload"),
        "evacuation_plan": ("incident_id",),
    }
    ACTION_REQUIRED = {
        ("worker", "rescue"): ("incident_id",),
        ("worker", "shelter"): ("refuge_id",),
        ("sensor", "mark_faulty"): ("reason",),
        ("ventilation", "restore"): ("tested_at",),
        ("ventilation", "degrade"): ("reason",),
        ("incident", "close"): ("summary",),
        ("task", "complete"): ("result",),
        ("task", "cancel"): ("reason",),
    }
    CREATE_ROLES = {
        "worker": ("admin", "safety", "dispatcher"),
        "sensor": ("admin", "safety", "field"),
        "ventilation": ("admin", "safety"),
        "passage": ("admin", "safety", "field"),
        "refuge": ("admin", "safety"),
        "incident": ("admin", "safety", "dispatcher"),
        "task": ("admin", "dispatcher", "safety"),
        "offline_record": ("admin", "safety", "dispatcher", "field"),
        "evacuation_plan": ("admin", "safety", "dispatcher"),
    }
    ROLE_ACTIONS = {
        "mark_missing": ("admin", "safety", "dispatcher"),
        "locate": ("admin", "field", "safety"),
        "evacuate": ("admin", "field", "dispatcher"),
        "rescue": ("admin", "field", "safety"),
        "find_safe": ("admin", "field", "safety"),
        "deactivate": ("admin", "safety"),
        "raise_warning": ("admin", "field", "safety"),
        "raise_alarm": ("admin", "field", "safety"),
        "clear": ("admin", "safety"),
        "mark_faulty": ("admin", "safety"),
        "verify_misread": ("admin", "safety"),
        "degrade": ("admin", "safety"),
        "stop": ("admin", "safety"),
        "restore": ("admin", "safety"),
        "restrict": ("admin", "safety", "field"),
        "block": ("admin", "safety", "field"),
        "clear": ("admin", "safety", "field"),
        "occupy": ("admin", "field", "safety"),
        "release": ("admin", "field", "safety"),
        "maintain": ("admin", "safety"),
        "reopen": ("admin", "safety"),
        "begin_evacuation": ("admin", "safety", "dispatcher"),
        "search": ("admin", "safety", "dispatcher"),
        "stabilize": ("admin", "safety", "dispatcher"),
        "recover": ("admin", "safety", "dispatcher"),
        "close": ("admin", "safety"),
        "assign": ("admin", "dispatcher", "safety"),
        "accept": ("admin", "field", "dispatcher"),
        "complete": ("admin", "field", "dispatcher"),
        "cancel": ("admin", "dispatcher", "safety"),
        ("worker", "shelter"): ("admin", "field", "safety", "dispatcher"),
        ("worker", "mark_awaiting_rescue"): ("admin", "safety", "dispatcher"),
        ("worker", "reroute"): ("admin", "safety", "dispatcher"),
        ("evacuation_plan", "submit"): ("admin", "safety", "dispatcher"),
        ("evacuation_plan", "cancel"): ("admin", "safety", "dispatcher"),
    }
    CUSTOM_CREATE = {
        "worker": lambda a, d, l: _validate_worker(d),
        "sensor": lambda a, d, l: _validate_sensor(d),
        "ventilation": lambda a, d, l: _validate_capacity(d, "capacity"),
        "passage": lambda a, d, l: _validate_passage(d),
        "refuge": lambda a, d, l: _validate_capacity(d, "capacity"),
        "incident": lambda a, d, l: _validate_incident(d),
        "task": lambda a, d, l: _validate_task(d, l),
        "offline_record": lambda a, d, l: _validate_offline(d),
        "evacuation_plan": lambda a, d, l: _validate_evacuation_plan(d, l),
    }
    CUSTOM_TRANSITIONS = {
        ("sensor", "raise_alarm"): _sensor_alarm,
        ("incident", "close"): _close_incident,
        ("task", "complete"): _complete_task,
        ("evacuation_plan", "submit"): _submit_evacuation_plan,
        ("worker", "shelter"): _worker_shelter,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def refuge_occupancy(self, lookup, exclude_plan_id=None):
        return _refuge_occupancy(lookup, exclude_plan_id)

    def compute_routes(self, lookup, workers, base_occupancy=None):
        return compute_routes(lookup, workers, base_occupancy)

    def initial_status(self, kind, data=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        _ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        _require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        _ensure_role(actor, allowed)
        _require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
