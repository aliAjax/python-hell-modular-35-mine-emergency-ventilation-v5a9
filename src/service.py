import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError, PermissionDenied, ValidationError
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
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "passage":
            self._recompute_plans(entity_id)
        elif kind == "worker" and action == "shelter":
            self._confirm_arrival(entity_id, patch.get("refuge_id"))
        return updated

    def _recompute_plans(self, passage_id):
        """Invalidate routes through a changed passage and re-route affected workers."""
        system = Actor("system", "admin")
        for summary in self.repository.list_entities(kind="evacuation_plan", status="active"):
            for _ in range(3):
                plan = self.repository.get_entity(summary["id"])
                kept, pending = [], []
                for assignment in plan["data"].get("assignments", []):
                    if assignment.get("status") == "arrived":
                        kept.append(assignment)
                    elif assignment.get("status") == "planned" and passage_id not in assignment.get("passage_ids", []):
                        kept.append(assignment)
                    else:
                        pending.append(assignment)
                if not pending:
                    break
                workers = []
                for assignment in pending:
                    worker = self.repository.get_entity(assignment["worker_id"])
                    if worker and worker["status"] in ("active", "awaiting_rescue"):
                        workers.append(worker)
                    else:
                        kept.append(assignment)
                occupancy = self.rules.refuge_occupancy(self._lookup, exclude_plan_id=plan["id"])
                for assignment in kept:
                    refuge_id = assignment.get("refuge_id")
                    if refuge_id:
                        occupancy[refuge_id] = occupancy.get(refuge_id, 0) + 1
                rerouted = self.rules.compute_routes(self._lookup, workers, occupancy)
                by_worker = {worker["id"]: worker for worker in workers}
                for assignment in rerouted:
                    worker = by_worker[assignment["worker_id"]]
                    if assignment["status"] == "planned" and worker["status"] == "awaiting_rescue":
                        self.transition(system, worker["id"], "reroute", {"plan_id": plan["id"]})
                    elif assignment["status"] == "unreachable" and worker["status"] == "active":
                        self.transition(system, worker["id"], "mark_awaiting_rescue", {"plan_id": plan["id"]})
                merged = dict(plan["data"])
                merged["assignments"] = kept + rerouted
                try:
                    self.repository.update_entity(plan["id"], plan["version"], plan["status"], merged)
                except ConflictError:
                    continue
                self.audit.record(
                    plan["id"],
                    system,
                    "recompute",
                    plan["status"],
                    plan["status"],
                    {"passage_id": passage_id, "reassigned": [a["worker_id"] for a in rerouted]},
                )
                break

    def _confirm_arrival(self, worker_id, refuge_id):
        """Mark the worker's planned assignment as arrived in the active plan."""
        system = Actor("system", "admin")
        for summary in self.repository.list_entities(kind="evacuation_plan", status="active"):
            for _ in range(3):
                plan = self.repository.get_entity(summary["id"])
                assignments = plan["data"].get("assignments", [])
                matched = False
                for assignment in assignments:
                    if (
                        assignment.get("worker_id") == worker_id
                        and assignment.get("status") == "planned"
                        and assignment.get("refuge_id") == refuge_id
                    ):
                        assignment["status"] = "arrived"
                        matched = True
                if not matched:
                    break
                merged = dict(plan["data"])
                merged["assignments"] = assignments
                try:
                    self.repository.update_entity(plan["id"], plan["version"], plan["status"], merged)
                except ConflictError:
                    continue
                self.audit.record(
                    plan["id"],
                    system,
                    "confirm_arrival",
                    plan["status"],
                    plan["status"],
                    {"worker_id": worker_id, "refuge_id": refuge_id},
                )
                return

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
