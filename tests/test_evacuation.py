import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class EvacuationPlanningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rules = RuleEngine()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), self.rules)
        self.admin = Actor("admin", "admin")
        self.dispatcher = Actor("dispatcher", "dispatcher")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data):
        return self.service.create(self.admin, kind, data)

    def act(self, entity, action, data=None, version=None):
        return self.service.transition(self.admin, entity["id"], action, data or {}, version)

    def open_incident(self, area="M-01"):
        incident = self.create("incident", {"area_code": area, "severity": "critical", "summary": "gas alarm"})
        incident = self.act(incident, "begin_evacuation")
        return incident

    def test_plan_routes_workers_to_refuges(self):
        incident = self.open_incident()
        worker = self.create("worker", {"name": "Li Wei", "location_code": "face-A", "team": "A"})
        refuge = self.create("refuge", {"location_code": "safe-haven", "capacity": 5})
        self.create("passage", {"from_location": "face-A", "to_location": "safe-haven", "width_m": 2.0})

        plan = self.service.submit_evacuation_plan(self.dispatcher, incident["id"], incident["version"])

        self.assertEqual(plan["data"]["incident_id"], incident["id"])
        route = plan["data"]["routes"][0]
        self.assertEqual(route["worker_id"], worker["id"])
        self.assertEqual(route["refuge_id"], refuge["id"])
        self.assertEqual(route["outcome"], "in_place")
        worker = self.service.get(worker["id"])
        self.assertEqual(worker["status"], "evacuated")
        self.assertEqual(worker["data"]["refuge_id"], refuge["id"])
        refuge = self.service.get(refuge["id"])
        self.assertEqual(refuge["status"], "occupied")
        self.assertEqual(refuge["data"]["occupied_count"], 1)

    def test_capacity_is_respected_and_overflow_becomes_pending_rescue(self):
        incident = self.open_incident()
        self.create("worker", {"name": "Wang Gang", "location_code": "face-A", "team": "A"})
        self.create("worker", {"name": "Zhao Min", "location_code": "face-A", "team": "B"})
        refuge = self.create("refuge", {"location_code": "safe-haven", "capacity": 1})
        self.create("passage", {"from_location": "face-A", "to_location": "safe-haven", "width_m": 2.0})

        plan = self.service.submit_evacuation_plan(self.dispatcher, incident["id"], incident["version"])

        in_place = [r for r in plan["data"]["routes"] if r["outcome"] == "in_place"]
        pending = [r for r in plan["data"]["routes"] if r["outcome"] == "pending_rescue"]
        self.assertEqual(len(in_place), 1)
        self.assertEqual(len(pending), 1)
        refuge = self.service.get(refuge["id"])
        self.assertEqual(refuge["data"]["occupied_count"], 1)
        pending_worker = self.service.get(pending[0]["worker_id"])
        self.assertEqual(pending_worker["status"], "pending_rescue")

    def test_blocked_passage_invalidates_routes_and_marks_pending_rescue(self):
        incident = self.open_incident()
        worker = self.create("worker", {"name": "Chen Lu", "location_code": "face-A", "team": "A"})
        refuge = self.create("refuge", {"location_code": "safe-haven", "capacity": 5})
        direct = self.create("passage", {"from_location": "face-A", "to_location": "safe-haven", "width_m": 2.0})

        plan = self.service.submit_evacuation_plan(self.dispatcher, incident["id"], incident["version"])
        self.assertEqual(plan["data"]["routes"][0]["outcome"], "in_place")

        self.act(direct, "block", {"reason": "roof fall"})

        worker = self.service.get(worker["id"])
        self.assertEqual(worker["status"], "pending_rescue")
        plans = self.service.list("evacuation_plan")
        route = plans[0]["data"]["routes"][0]
        self.assertEqual(route["outcome"], "pending_rescue")
        self.assertEqual(route["status"], "invalidated")

    def test_detour_keeps_worker_in_place_after_block(self):
        incident = self.open_incident()
        worker = self.create("worker", {"name": "Sun Ke", "location_code": "face-A", "team": "A"})
        refuge = self.create("refuge", {"location_code": "safe-haven", "capacity": 5})
        direct = self.create("passage", {"from_location": "face-A", "to_location": "safe-haven", "width_m": 2.0})
        self.create("passage", {"from_location": "face-A", "to_location": "junction", "width_m": 2.0})
        self.create("passage", {"from_location": "junction", "to_location": "safe-haven", "width_m": 2.0})

        plan = self.service.submit_evacuation_plan(self.dispatcher, incident["id"], incident["version"])
        self.assertEqual(len(plan["data"]["routes"][0]["path"]), 1)

        self.act(direct, "block", {"reason": "roof fall"})

        worker = self.service.get(worker["id"])
        self.assertEqual(worker["status"], "evacuated")
        plans = self.service.list("evacuation_plan")
        route = plans[0]["data"]["routes"][0]
        self.assertEqual(route["outcome"], "in_place")
        self.assertEqual(len(route["path"]), 2)

    def test_reopen_passage_rescues_pending_worker(self):
        incident = self.open_incident()
        worker = self.create("worker", {"name": "Zhou Tao", "location_code": "face-A", "team": "A"})
        refuge = self.create("refuge", {"location_code": "safe-haven", "capacity": 5})
        direct = self.create("passage", {"from_location": "face-A", "to_location": "safe-haven", "width_m": 2.0})

        self.service.submit_evacuation_plan(self.dispatcher, incident["id"], incident["version"])
        self.act(direct, "block", {"reason": "roof fall"})
        self.assertEqual(self.service.get(worker["id"])["status"], "pending_rescue")

        self.act(direct, "clear", {})

        worker = self.service.get(worker["id"])
        self.assertEqual(worker["status"], "evacuated")

    def test_concurrent_submit_loses_conflict(self):
        incident = self.open_incident()
        self.create("worker", {"name": "Wu Lei", "location_code": "face-A", "team": "A"})
        self.create("refuge", {"location_code": "safe-haven", "capacity": 5})
        self.create("passage", {"from_location": "face-A", "to_location": "safe-haven", "width_m": 2.0})

        version = incident["version"]
        first = self.service.submit_evacuation_plan(self.dispatcher, incident["id"], version)
        self.assertIsNotNone(first)
        with self.assertRaises(ConflictError):
            self.service.submit_evacuation_plan(self.dispatcher, incident["id"], version)

    def test_submit_requires_evacuating_status(self):
        incident = self.create("incident", {"area_code": "M-02", "severity": "high", "summary": "alarm"})
        with self.assertRaises(ValidationError):
            self.service.submit_evacuation_plan(self.dispatcher, incident["id"], incident["version"])

    def test_close_blocked_until_workers_accounted_for(self):
        # Rule-level gate: an on-duty (active) worker blocks close even when
        # the incident has reached the recovering state.
        incident = {"id": "i-1", "kind": "incident", "status": "recovering", "data": {}}
        active_worker = {"id": "w-1", "kind": "worker", "status": "active", "data": {}}
        safe_worker = {"id": "w-2", "kind": "worker", "status": "evacuated", "data": {}}

        def lookup_with_active(kind, field, value):
            if kind == "worker":
                return [active_worker, safe_worker]
            return [incident] if kind == "incident" else []

        with self.assertRaises(ConflictError):
            self.rules.validate_transition(self.admin, incident, "close", {"summary": "done"}, lookup_with_active)

        def lookup_all_accounted(kind, field, value):
            if kind == "worker":
                return [safe_worker]
            return [incident] if kind == "incident" else []

        next_status, _ = self.rules.validate_transition(
            self.admin, incident, "close", {"summary": "done"}, lookup_all_accounted
        )
        self.assertEqual(next_status, "closed")

    def test_close_succeeds_after_plan(self):
        incident = self.open_incident()
        self.create("worker", {"name": "Zheng Fei", "location_code": "face-A", "team": "A"})
        self.create("refuge", {"location_code": "safe-haven", "capacity": 5})
        self.create("passage", {"from_location": "face-A", "to_location": "safe-haven", "width_m": 2.0})
        self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 10})

        self.service.submit_evacuation_plan(self.dispatcher, incident["id"], incident["version"])
        for action in ("search", "stabilize", "recover"):
            incident = self.act(incident, action)
        incident = self.act(incident, "close", {"summary": "all in place"})
        self.assertEqual(incident["status"], "closed")


if __name__ == "__main__":
    unittest.main()
