import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class EvacuationPlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data):
        return self.service.create(self.admin, kind, data)

    def act(self, entity, action, data=None, version=None, actor=None):
        return self.service.transition(actor or self.admin, entity["id"], action, data or {}, version)

    def alarmed_incident(self):
        incident = self.create("incident", {"area_code": "M-01", "severity": "critical", "summary": "gas leak"})
        sensor = self.create("sensor", {"location_code": "W1", "gas_ppm": 120, "threshold_ppm": 80})
        self.act(sensor, "raise_alarm")
        return incident

    def test_plan_requires_gas_alarm(self):
        incident = self.create("incident", {"area_code": "M-01", "severity": "high", "summary": "x"})
        with self.assertRaises(ValidationError):
            self.create("evacuation_plan", {"incident_id": incident["id"]})

    def test_plan_requires_open_incident(self):
        self.create("sensor", {"location_code": "W1", "gas_ppm": 120, "threshold_ppm": 80})
        with self.assertRaises(ValidationError):
            self.create("evacuation_plan", {"incident_id": "no-such-incident"})

    def test_duplicate_plan_for_incident_is_rejected(self):
        incident = self.alarmed_incident()
        self.create("evacuation_plan", {"incident_id": incident["id"]})
        with self.assertRaises(ConflictError):
            self.create("evacuation_plan", {"incident_id": incident["id"]})

    def test_submit_computes_routes_for_on_duty_workers(self):
        incident = self.alarmed_incident()
        p1 = self.create("passage", {"from_location": "W1", "to_location": "J1", "width_m": 2})
        p2 = self.create("passage", {"from_location": "J1", "to_location": "R1", "width_m": 2})
        refuge = self.create("refuge", {"location_code": "R1", "capacity": 5})
        worker = self.create("worker", {"name": "Li Wei", "location_code": "W1", "team": "A"})
        plan = self.create("evacuation_plan", {"incident_id": incident["id"]})
        self.assertEqual(plan["status"], "draft")

        plan = self.act(plan, "submit")
        self.assertEqual(plan["status"], "active")
        assignments = plan["data"]["assignments"]
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0]["worker_id"], worker["id"])
        self.assertEqual(assignments[0]["refuge_id"], refuge["id"])
        self.assertEqual(assignments[0]["passage_ids"], [p1["id"], p2["id"]])
        self.assertEqual(assignments[0]["status"], "planned")

    def test_submit_refuses_arrangement_beyond_refuge_capacity(self):
        incident = self.alarmed_incident()
        self.create("passage", {"from_location": "W1", "to_location": "R1", "width_m": 2})
        self.create("refuge", {"location_code": "R1", "capacity": 1})
        self.create("worker", {"name": "Li Wei", "location_code": "W1", "team": "A"})
        self.create("worker", {"name": "Wang Fang", "location_code": "W1", "team": "A"})
        plan = self.create("evacuation_plan", {"incident_id": incident["id"]})

        plan = self.act(plan, "submit")
        assignments = plan["data"]["assignments"]
        planned = [a for a in assignments if a["status"] == "planned"]
        refused = [a for a in assignments if a["status"] == "unassigned"]
        self.assertEqual(len(planned), 1)
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["reason"], "refuge_capacity_exceeded")
        # 容量拒绝只影响安排本身，人员在岗状态不变
        for worker in self.service.list("worker"):
            self.assertEqual(worker["status"], "active")

    def test_passage_change_invalidates_and_recomputes_routes(self):
        incident = self.alarmed_incident()
        p1 = self.create("passage", {"from_location": "W1", "to_location": "J1", "width_m": 2})
        p2 = self.create("passage", {"from_location": "J1", "to_location": "R1", "width_m": 2})
        worker = self.create("worker", {"name": "Li Wei", "location_code": "W1", "team": "A"})
        refuge = self.create("refuge", {"location_code": "R1", "capacity": 5})
        plan = self.create("evacuation_plan", {"incident_id": incident["id"]})
        plan = self.act(plan, "submit")
        self.assertEqual(plan["data"]["assignments"][0]["passage_ids"], [p1["id"], p2["id"]])

        # 新开一条备用通道后阻断原路线第一段
        p3 = self.create("passage", {"from_location": "W1", "to_location": "R1", "width_m": 2})
        self.act(p1, "block")
        plan = self.service.get(plan["id"])
        assignment = plan["data"]["assignments"][0]
        self.assertEqual(assignment["status"], "planned")
        self.assertEqual(assignment["passage_ids"], [p3["id"]])
        self.assertEqual(assignment["refuge_id"], refuge["id"])
        self.assertEqual(self.service.get(worker["id"])["status"], "active")

    def test_unrouteable_workers_are_marked_awaiting_rescue(self):
        incident = self.alarmed_incident()
        p1 = self.create("passage", {"from_location": "W1", "to_location": "J1", "width_m": 2})
        self.create("passage", {"from_location": "J1", "to_location": "R1", "width_m": 2})
        self.create("refuge", {"location_code": "R1", "capacity": 5})
        worker = self.create("worker", {"name": "Li Wei", "location_code": "W1", "team": "A"})
        plan = self.create("evacuation_plan", {"incident_id": incident["id"]})
        plan = self.act(plan, "submit")

        self.act(p1, "block")
        worker = self.service.get(worker["id"])
        self.assertEqual(worker["status"], "awaiting_rescue")
        plan = self.service.get(plan["id"])
        assignment = plan["data"]["assignments"][0]
        self.assertEqual(assignment["status"], "unreachable")
        self.assertEqual(assignment["reason"], "no_route")

        # 待救援人员可以直接转为已救出
        worker = self.act(worker, "rescue", {"incident_id": incident["id"]})
        self.assertEqual(worker["status"], "rescued")

    def test_cleared_passage_reroutes_awaiting_workers(self):
        incident = self.alarmed_incident()
        p1 = self.create("passage", {"from_location": "W1", "to_location": "J1", "width_m": 2})
        p2 = self.create("passage", {"from_location": "J1", "to_location": "R1", "width_m": 2})
        self.create("refuge", {"location_code": "R1", "capacity": 5})
        worker = self.create("worker", {"name": "Li Wei", "location_code": "W1", "team": "A"})
        plan = self.create("evacuation_plan", {"incident_id": incident["id"]})
        plan = self.act(plan, "submit")

        self.act(p1, "block")
        self.assertEqual(self.service.get(worker["id"])["status"], "awaiting_rescue")
        self.act(p1, "clear")
        worker = self.service.get(worker["id"])
        self.assertEqual(worker["status"], "active")
        plan = self.service.get(plan["id"])
        assignment = plan["data"]["assignments"][0]
        self.assertEqual(assignment["status"], "planned")
        self.assertEqual(assignment["passage_ids"], [p1["id"], p2["id"]])

    def test_concurrent_submit_loser_gets_conflict(self):
        incident = self.alarmed_incident()
        self.create("refuge", {"location_code": "R1", "capacity": 5})
        plan = self.create("evacuation_plan", {"incident_id": incident["id"]})

        results, errors = [], []

        def submit(user):
            try:
                self.service.transition(Actor(user, "dispatcher"), plan["id"], "submit", {}, plan["version"])
                results.append(user)
            except (ConflictError, InvalidTransition) as exc:
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=("u%d" % n,)) for n in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.service.get(plan["id"])["status"], "active")

    def test_stale_version_on_plan_action_is_rejected(self):
        incident = self.alarmed_incident()
        plan = self.create("evacuation_plan", {"incident_id": incident["id"]})
        with self.assertRaises(ConflictError):
            self.act(plan, "cancel", {"reason": "stale"}, version=plan["version"] + 5)

    def test_shelter_marks_arrival_and_requires_assignment(self):
        incident = self.alarmed_incident()
        self.create("passage", {"from_location": "W1", "to_location": "R1", "width_m": 2})
        refuge = self.create("refuge", {"location_code": "R1", "capacity": 1})
        other = self.create("refuge", {"location_code": "R2", "capacity": 5})
        worker = self.create("worker", {"name": "Li Wei", "location_code": "W1", "team": "A"})
        stranded = self.create("worker", {"name": "Wang Fang", "location_code": "W1", "team": "A"})
        plan = self.create("evacuation_plan", {"incident_id": incident["id"]})
        plan = self.act(plan, "submit")

        planned = [a for a in plan["data"]["assignments"] if a["status"] == "planned"]
        self.assertEqual(len(planned), 1)
        arrived_worker = worker if planned[0]["worker_id"] == worker["id"] else stranded
        waiting_worker = stranded if arrived_worker["id"] == worker["id"] else worker

        # 没有规划安排的硐室不能登记到位
        with self.assertRaises(ValidationError):
            self.act(arrived_worker, "shelter", {"refuge_id": other["id"]})
        # 容量外未安排的人员不能登记到位
        with self.assertRaises(ValidationError):
            self.act(waiting_worker, "shelter", {"refuge_id": refuge["id"]})

        arrived_worker = self.act(arrived_worker, "shelter", {"refuge_id": refuge["id"]})
        self.assertEqual(arrived_worker["status"], "sheltered")
        plan = self.service.get(plan["id"])
        arrived = [a for a in plan["data"]["assignments"] if a["worker_id"] == arrived_worker["id"]]
        self.assertEqual(arrived[0]["status"], "arrived")

    def test_close_requires_on_duty_workers_sheltered_or_awaiting_rescue(self):
        incident = self.alarmed_incident()
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.act(incident, action)
        p1 = self.create("passage", {"from_location": "W1", "to_location": "R1", "width_m": 2})
        refuge = self.create("refuge", {"location_code": "R1", "capacity": 5})
        worker = self.create("worker", {"name": "Li Wei", "location_code": "W1", "team": "A"})

        with self.assertRaises(ConflictError):
            self.act(incident, "close", {"summary": "done"})

        plan = self.create("evacuation_plan", {"incident_id": incident["id"]})
        plan = self.act(plan, "submit")
        worker = self.act(worker, "shelter", {"refuge_id": refuge["id"]})
        incident = self.act(incident, "close", {"summary": "all clear"})
        self.assertEqual(incident["status"], "closed")

    def test_close_allowed_with_awaiting_rescue_workers(self):
        incident = self.alarmed_incident()
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.act(incident, action)
        p1 = self.create("passage", {"from_location": "W1", "to_location": "R1", "width_m": 2})
        self.create("refuge", {"location_code": "R1", "capacity": 5})
        self.create("worker", {"name": "Li Wei", "location_code": "W1", "team": "A"})
        plan = self.create("evacuation_plan", {"incident_id": incident["id"]})
        self.act(plan, "submit")
        self.act(p1, "block")

        incident = self.act(incident, "close", {"summary": "rescue handed over"})
        self.assertEqual(incident["status"], "closed")


if __name__ == "__main__":
    unittest.main()
