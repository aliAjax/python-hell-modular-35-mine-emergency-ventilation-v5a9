import hashlib
from uuid import uuid4

from collections import deque
from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError, PermissionDenied, ValidationError
from .repository import utcnow
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if entity["kind"] == "passage":
            self._replan_after_passage_change(updated)
        return updated

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            existing = self.repository.get_entity(entity_id)
            if existing:
                created.append(existing)
                continue
            payload = dict(raw)
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            entity = self.repository.create_entity(
                entity_id,
                "offline_record",
                self.rules.initial_status("offline_record", payload),
                payload,
                actor.user_id,
            )
            self.audit.record(entity_id, actor, "merge_offline", None, entity["status"], {"source_id": source_id, "record_id": record_id})
            created.append(entity)
        return created

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ------------------------------------------------------------------
    # Evacuation planning
    # ------------------------------------------------------------------
    def submit_evacuation_plan(self, actor, incident_id, expected_version=None):
        incident = self.repository.get_entity(incident_id)
        if not incident:
            raise NotFoundError("incident not found: " + incident_id)
        if incident["status"] != "evacuating":
            raise ValidationError("evacuation plan requires an incident under evacuation")
        if expected_version is None:
            raise ValidationError("expected_version is required to submit an evacuation plan")
        arrangement = self._compute_arrangement(incident)
        # Atomic version bump: two concurrent submissions cannot both succeed.
        self.repository.update_entity(incident_id, expected_version, incident["status"], incident["data"])
        return self._persist_arrangement(incident, actor, arrangement, supersede=True, plan_id=None)

    def _replan_after_passage_change(self, passage):
        # Any passage change (block/restrict/clear) can open or close detours,
        # so every active plan is recomputed from the current passage graph.
        plans = self.repository.list_entities("evacuation_plan", status="active")
        if not plans:
            return
        system = Actor("system", "admin")
        for plan in plans:
            incident = self.repository.get_entity(plan["data"].get("incident_id"))
            if not incident:
                continue
            arrangement = self._compute_arrangement(incident, release_plan=plan)
            self._persist_arrangement(incident, system, arrangement, supersede=False, plan_id=plan["id"])
            self.repository.update_entity(incident["id"], None, incident["status"], incident["data"])
            self.audit.record(
                passage["id"],
                system,
                "replan_routes",
                "active",
                "active",
                {"plan_id": plan["id"], "passage_id": passage["id"]},
            )

    def _compute_arrangement(self, incident, release_plan=None):
        workers = [
            worker for worker in self.repository.list_entities("worker")
            if worker["status"] in ("active", "evacuated", "pending_rescue")
        ]
        passages = self.repository.list_entities("passage")
        refuges = [
            refuge for refuge in self.repository.list_entities("refuge")
            if refuge["status"] != "maintenance"
        ]
        adjacency = {}
        for passage in passages:
            if passage["status"] not in ("open", "restricted"):
                continue
            data = passage["data"]
            adjacency.setdefault(data["from_location"], []).append((passage["id"], data["to_location"]))
            adjacency.setdefault(data["to_location"], []).append((passage["id"], data["from_location"]))

        capacity = {}
        occupied = {}
        for refuge in refuges:
            capacity[refuge["id"]] = float(refuge["data"].get("capacity", 0) or 0)
            occupied[refuge["id"]] = int(refuge["data"].get("occupied_count", 0) or 0)
        if release_plan:
            for route in release_plan["data"].get("routes", []):
                refuge_id = route.get("refuge_id")
                if refuge_id and refuge_id in occupied:
                    occupied[refuge_id] = max(0, occupied[refuge_id] - 1)

        refuges_at = {}
        for refuge in refuges:
            refuges_at.setdefault(refuge["data"]["location_code"], []).append(refuge)

        assignments = []
        unroutable = []
        for worker in workers:
            start = worker["data"].get("evacuation_from") or worker["data"].get("location_code")
            found = self._find_route(start, refuges_at, adjacency, capacity, occupied)
            if found:
                refuge, path = found
                occupied[refuge["id"]] += 1
                assignments.append((worker, refuge, path, start))
            else:
                unroutable.append(worker)
        return {"assignments": assignments, "unroutable": unroutable, "occupied": occupied}

    def _find_route(self, start, refuges_at, adjacency, capacity, occupied):
        def available_refuge(location):
            for refuge in refuges_at.get(location, []):
                if occupied.get(refuge["id"], 0) < capacity.get(refuge["id"], 0):
                    return refuge
            return None

        if start is None:
            return None
        refuge = available_refuge(start)
        if refuge:
            return refuge, []
        visited = {start}
        queue = deque([(start, [])])
        while queue:
            location, path = queue.popleft()
            for passage_id, neighbor in adjacency.get(location, []):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                new_path = path + [passage_id]
                refuge = available_refuge(neighbor)
                if refuge:
                    return refuge, new_path
                queue.append((neighbor, new_path))
        return None

    def _persist_arrangement(self, incident, actor, arrangement, supersede, plan_id):
        if supersede:
            for old in self.repository.list_entities("evacuation_plan", status="active"):
                if old["data"].get("incident_id") == incident["id"]:
                    self.repository.update_entity(old["id"], None, "superseded", old["data"])

        routes = []
        for worker, refuge, path, start in arrangement["assignments"]:
            routes.append({
                "worker_id": worker["id"],
                "from_location": start,
                "refuge_id": refuge["id"],
                "path": path,
                "status": "active",
                "outcome": "in_place",
            })
            worker_data = dict(worker["data"])
            worker_data["evacuation_from"] = worker_data.get("evacuation_from") or start
            worker_data["location_code"] = refuge["data"]["location_code"]
            worker_data["refuge_id"] = refuge["id"]
            self.repository.update_entity(worker["id"], None, "evacuated", worker_data)

        for worker in arrangement["unroutable"]:
            routes.append({
                "worker_id": worker["id"],
                "from_location": worker["data"].get("evacuation_from") or worker["data"].get("location_code"),
                "refuge_id": None,
                "path": [],
                "status": "invalidated",
                "outcome": "pending_rescue",
            })
            worker_data = dict(worker["data"])
            worker_data["evacuation_from"] = worker_data.get("evacuation_from") or worker_data.get("location_code")
            worker_data["refuge_id"] = None
            self.repository.update_entity(worker["id"], None, "pending_rescue", worker_data)

        for refuge in self.repository.list_entities("refuge"):
            new_occupied = int(arrangement["occupied"].get(refuge["id"], 0) or 0)
            status = "occupied" if new_occupied > 0 else "available"
            refuge_data = dict(refuge["data"])
            refuge_data["occupied_count"] = new_occupied
            self.repository.update_entity(refuge["id"], None, status, refuge_data)

        plan_data = {
            "incident_id": incident["id"],
            "routes": routes,
            "submitted_by": actor.user_id,
            "submitted_at": utcnow(),
        }
        if plan_id:
            plan = self.repository.update_entity(plan_id, None, "active", plan_data)
        else:
            plan = self.repository.create_entity(
                str(uuid4()), "evacuation_plan", "active", plan_data, actor.user_id
            )
        self.audit.record(
            plan["id"],
            actor,
            "submit_evacuation_plan" if not plan_id else "replan_evacuation_plan",
            None,
            "active",
            {
                "incident_id": incident["id"],
                "in_place": len(arrangement["assignments"]),
                "pending_rescue": len(arrangement["unroutable"]),
            },
        )
        return plan
