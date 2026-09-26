from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from evacuation.api import JsonApplication
from evacuation.clock import FrozenClock
from evacuation.errors import Conflict, Forbidden, InvalidState, NotFound
from evacuation.planning import build_return_plan, route_state_at
from evacuation.service import EvacuationService


def warning(version=1, level="orange", deadline="2026-09-26T12:00:00Z",
            earliest_return="2026-09-27T08:00:00Z", issued_at="2026-09-26T06:00:00Z"):
    return {
        "warning_id": "w1",
        "version": version,
        "issued_at": issued_at,
        "commit_deadline_at": deadline,
        "earliest_return_at": earliest_return,
        "level": level,
        "hazard": "滑坡",
        "affected_villages": ["v1"],
        "risk_curve": [
            {"observed_at": issued_at, "risk_level": "yellow"},
            {"observed_at": deadline[:11] + "09:00:00Z", "risk_level": level},
        ],
    }


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 6, 0, tzinfo=timezone.utc))
        self.service = EvacuationService(self.connection, self.clock)
        for user_id, role in (
            ("risk", "risk"), ("planner", "planner"), ("director", "director"),
            ("dispatch", "dispatcher"), ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.issue_warning("risk", warning())
        self.service.register_village("planner", {"village_id": "v1", "name": "一村"})

    def tearDown(self) -> None:
        self.connection.close()

    def household(self, number, *, members=3, mobility=0, children=0, medical=0,
                  dangerous=True, transport=True, village="v1"):
        self.service.upsert_household("planner", {
            "household_id": f"h{number}", "village_id": village, "head_name": f"户{number}",
            "members": members, "mobility_impaired": mobility, "school_children": children,
            "special_medical": medical, "dangerous_house": dangerous,
            "transport_required": transport,
        })

    def resources(self, beds=100, medical_beds=10, seats=20, wheelchair=5):
        self.service.register_vehicle("planner", {
            "vehicle_id": "bus", "plate": "晋A1", "seats": seats, "wheelchair_seats": wheelchair})
        self.service.register_shelter("planner", {
            "shelter_id": "sh", "name": "安置点", "beds_total": beds, "medical_beds_total": medical_beds})
        self.service.register_route("planner", {
            "route_id": "r1", "village_id": "v1", "shelter_id": "sh",
            "minutes": 30, "throughput_per_hour": 100})

    def plan(self, plan_id="p1", start="2026-09-26T06:30:00Z", warning_id="w1"):
        return self.service.generate_plan("planner", {
            "plan_id": plan_id, "warning_id": warning_id, "evacuation_start": start})


class BaselineAndWarningTests(ServiceTestBase):
    def test_warning_versions_must_be_contiguous(self) -> None:
        with self.assertRaises(Conflict):
            self.service.issue_warning("risk", warning(version=3))

    def test_role_permissions_enforced(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.issue_warning("planner", warning(version=2))
        with self.assertRaises(Forbidden):
            self.service.register_village("risk", {"village_id": "x", "name": "x"})

    def test_unknown_village_rejects_household(self) -> None:
        with self.assertRaises(NotFound):
            self.household(1, village="missing")

    def test_special_counts_cannot_exceed_members(self) -> None:
        from evacuation.errors import ValidationFailed
        with self.assertRaises(ValidationFailed):
            self.household(1, members=2, mobility=3)


class PlanningTests(ServiceTestBase):
    def test_vulnerable_and_dangerous_households_evacuate_first(self) -> None:
        self.household(1, dangerous=False)
        self.household(2, children=1)
        self.resources()
        result = self.plan()
        chosen = next(c for c in result["candidates"] if c["candidate_id"] == "safety_first")
        detail = self.service.candidate_detail("p1", "safety_first")
        stage1 = {b["household_id"] for b in detail["stages"][0]["batches"]}
        self.assertIn("h2", stage1)
        self.assertNotIn("h1", stage1)
        self.assertTrue(chosen["feasible"])

    def test_closed_road_makes_candidates_infeasible(self) -> None:
        self.household(1)
        self.resources()
        self.service.report_road_status("risk", "r1", {
            "state": "closed", "effective_at": "2026-09-26T06:00:00Z", "note": "塌方"})
        result = self.plan()
        self.assertFalse(any(c["feasible"] for c in result["candidates"]))
        self.assertTrue(any("road_closed:r1" in c["violations"] for c in result["candidates"]))

    def test_route_status_merges_by_effective_time(self) -> None:
        events = [
            {"event_id": 1, "route_id": "r", "effective_at": "2026-09-26T06:00:00Z", "state": "closed"},
            {"event_id": 2, "route_id": "r", "effective_at": "2026-09-26T08:00:00Z", "state": "open"},
            {"event_id": 3, "route_id": "r", "effective_at": "2026-09-26T07:00:00Z", "state": "restricted"},
        ]
        self.assertEqual(route_state_at(events, "2026-09-26T07:30:00Z"), "restricted")
        self.assertEqual(route_state_at(events, "2026-09-26T08:30:00Z"), "open")

    def test_bed_shortfall_makes_candidate_infeasible(self) -> None:
        self.household(1, members=50)
        self.resources(beds=10, medical_beds=10, seats=60)
        result = self.plan()
        self.assertFalse(any(c["feasible"] for c in result["candidates"]))
        self.assertTrue(any(any(v.startswith("beds:") for v in c["violations"]) for c in result["candidates"]))

    def test_medical_bed_shortfall_makes_candidate_infeasible(self) -> None:
        self.household(1, members=5, medical=5)
        self.resources(beds=100, medical_beds=2)
        result = self.plan()
        self.assertFalse(any(c["feasible"] for c in result["candidates"]))
        self.assertTrue(any(any(v.startswith("medical_beds:") for v in c["violations"]) for c in result["candidates"]))

    def test_vehicle_capacity_shortfall_infeasible(self) -> None:
        self.household(1, members=30)
        self.resources(seats=10)
        result = self.plan()
        self.assertFalse(any(c["feasible"] for c in result["candidates"]))

    def test_wheelchair_constraint_assigns_accessible_vehicle(self) -> None:
        self.household(1, members=2, mobility=2)
        self.resources(seats=20, wheelchair=5)
        self.plan()
        detail = self.service.candidate_detail("p1", "safety_first")
        self.assertEqual(detail["stages"][0]["batches"][0]["vehicle_id"], "bus")
        trip = detail["vehicle_trips"][0]
        self.assertEqual(trip["wheelchair_used"], 2)

    def test_arrival_past_deadline_is_infeasible(self) -> None:
        self.household(1)
        self.resources()
        # 11:59 发车、30 分钟车程，12:29 才能到达，超出 12:00 承诺窗口。
        result = self.plan(start="2026-09-26T11:59:00Z")
        self.assertFalse(any(c["feasible"] for c in result["candidates"]))
        self.assertIn("deadline_exceeded", result["candidates"][0]["violations"])

    def test_deterministic_candidates(self) -> None:
        self.household(1, members=4)
        self.household(2, members=2, children=1)
        self.resources()
        first = self.plan()
        second = self.service.generate_plan("planner", {
            "plan_id": "p2", "warning_id": "w1", "evacuation_start": "2026-09-26T06:30:00Z"})
        self.assertEqual(
            json.dumps(first["candidates"], sort_keys=True, ensure_ascii=False),
            json.dumps(second["candidates"], sort_keys=True, ensure_ascii=False),
        )

    def test_return_plan_respects_window_and_order(self) -> None:
        plan = build_return_plan(
            trips=[{"vehicle_id": "b", "route_id": "r", "shelter_id": "s",
                    "arrive_at": "2026-09-26T07:00:00Z", "household_ids": ["h1"]}],
            self_transport_ids={"h2"},
            earliest_return="2026-09-27T08:00:00Z",
            interval_minutes=60,
        )
        self.assertGreaterEqual(plan["stages"][0]["depart_at"], "2026-09-27T08:00:00Z")


class ConfirmationAndFreezeTests(ServiceTestBase):
    def test_only_director_confirms_and_infeasible_rejected(self) -> None:
        self.household(1, members=30)
        self.resources(seats=5)
        self.plan()
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("director", {
                "plan_id": "p1", "candidate_id": "safety_first",
                "expected_revision": 1, "idempotency_key": "k"})

    def test_planner_cannot_confirm(self) -> None:
        self.household(1)
        self.resources()
        self.plan()
        with self.assertRaises(Forbidden):
            self.service.confirm_plan("planner", {
                "plan_id": "p1", "candidate_id": "safety_first",
                "expected_revision": 1, "idempotency_key": "k"})

    def test_confirmation_freezes_resources_against_other_plans(self) -> None:
        self.household(1, members=10)
        self.resources(beds=10)
        self.plan()
        self.service.confirm_plan("director", {
            "plan_id": "p1", "candidate_id": "safety_first",
            "expected_revision": 1, "idempotency_key": "k1"})
        # 同一窗口第二个方案，使用同一辆车和同一安置点，必然不可行。
        self.service.register_village("planner", {"village_id": "v2", "name": "二村"})
        self.service.register_route("planner", {
            "route_id": "r2", "village_id": "v2", "shelter_id": "sh",
            "minutes": 30, "throughput_per_hour": 100})
        self.service.upsert_household("planner", {
            "household_id": "h9", "village_id": "v2", "head_name": "户9",
            "members": 5, "dangerous_house": True})
        self.service.issue_warning("risk", {
            **warning(version=1), "warning_id": "w2", "affected_villages": ["v2"]})
        result = self.service.generate_plan("planner", {
            "plan_id": "p2", "warning_id": "w2", "evacuation_start": "2026-09-26T06:31:00Z"})
        self.assertFalse(any(c["feasible"] for c in result["candidates"]))

    def test_confirm_idempotent_replay(self) -> None:
        self.household(1)
        self.resources()
        self.plan()
        payload = {"plan_id": "p1", "candidate_id": "safety_first",
                   "expected_revision": 1, "idempotency_key": "k1"}
        first = self.service.confirm_plan("director", payload)
        second = self.service.confirm_plan("director", payload)
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.service.confirm_plan("director", {**payload, "candidate_id": "fast_clearance"})


class ReceiptTests(ServiceTestBase):
    def _confirmed(self):
        self.household(1, members=3, mobility=1)
        self.resources()
        self.plan()
        self.service.confirm_plan("director", {
            "plan_id": "p1", "candidate_id": "safety_first",
            "expected_revision": 1, "idempotency_key": "k"})

    def _receipt(self, event_type, observed_at, key, *, headcount=None, household="h1"):
        return self.service.record_receipt("dispatch", {
            "plan_id": "p1", "household_id": household, "event_type": event_type,
            "observed_at": observed_at, "headcount": headcount,
            "idempotency_key": key, "reporter": "网格员"})

    def test_duplicate_receipt_is_merged(self) -> None:
        self._confirmed()
        first = self._receipt("arrived", "2026-09-26T07:50:00Z", "r1", headcount=3)
        second = self._receipt("arrived", "2026-09-26T07:50:00Z", "r2", headcount=3)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertFalse(second["applied"])

    def test_out_of_order_older_event_does_not_advance_state(self) -> None:
        self._confirmed()
        self._receipt("arrived", "2026-09-26T08:00:00Z", "r1", headcount=3)
        late = self._receipt("departed", "2026-09-26T07:00:00Z", "r2")
        self.assertFalse(late["applied"])
        self.assertEqual(late["current_state"]["event_type"], "arrived")

    def test_terminal_state_is_protected(self) -> None:
        self._confirmed()
        self._receipt("arrived", "2026-09-26T08:00:00Z", "r1", headcount=3)
        # 更晚的“出发”回执也不能把终态回退。
        later = self._receipt("departed", "2026-09-26T09:00:00Z", "r2")
        self.assertFalse(later["applied"])
        self.assertEqual(later["current_state"]["event_type"], "arrived")

    def test_returned_after_arrived_advances(self) -> None:
        self._confirmed()
        self._receipt("arrived", "2026-09-26T08:00:00Z", "r1", headcount=3)
        ret = self._receipt("returned", "2026-09-27T09:00:00Z", "r2", headcount=3)
        self.assertTrue(ret["applied"])
        self.assertEqual(ret["current_state"]["event_type"], "returned")

    def test_receipts_rejected_before_confirmation(self) -> None:
        self.household(1)
        self.resources()
        self.plan()
        with self.assertRaises(InvalidState):
            self._receipt("departed", "2026-09-26T07:00:00Z", "r1")

    def test_exempt_terminal_counts_as_done(self) -> None:
        self._confirmed()
        self._receipt("exempt", "2026-09-26T07:30:00Z", "r1", headcount=3)
        explanation = self.service.explain_plan("dispatch", "p1")
        self.assertEqual(explanation["state"], "completed")
        self.assertEqual(explanation["people"]["exempt"], 3)


class RevisionTests(ServiceTestBase):
    def _setup_and_start_stage_one(self):
        self.household(1, members=3, mobility=1)
        self.household(2, members=2, dangerous=False)
        self.resources()
        self.plan()
        self.service.confirm_plan("director", {
            "plan_id": "p1", "candidate_id": "safety_first",
            "expected_revision": 1, "idempotency_key": "k1"})
        for index, (event_type, observed_at) in enumerate((
            ("notified", "2026-09-26T07:05:00Z"),
            ("departed", "2026-09-26T07:20:00Z"),
            ("arrived", "2026-09-26T07:50:00Z"),
        ), start=1):
            self.service.record_receipt("dispatch", {
                "plan_id": "p1", "household_id": "h1", "event_type": event_type,
                "observed_at": observed_at, "headcount": 3 if event_type == "arrived" else None,
                "idempotency_key": f"old-{index}"})
        self.service.issue_warning("risk", warning(
            version=2, level="red", deadline="2026-09-26T14:00:00Z",
            earliest_return="2026-09-27T12:00:00Z", issued_at="2026-09-26T08:30:00Z"))

    def test_revision_only_replans_unstarted_stage(self) -> None:
        self._setup_and_start_stage_one()
        revised = self.service.revise_for_warning("planner", {
            "plan_id": "p1", "expected_revision": 1,
            "evacuation_start": "2026-09-26T08:30:00Z"})
        self.assertEqual(revised["revision"], 2)
        detail = self.service.candidate_detail("p1", "safety_first")
        frozen = [s for s in detail["stages"] if s.get("frozen")]
        self.assertEqual(len(frozen), 1)
        self.assertEqual({b["household_id"] for b in frozen[0]["batches"]}, {"h1"})
        remaining = {b["household_id"] for s in detail["stages"] if not s.get("frozen") for b in s["batches"]}
        self.assertEqual(remaining, {"h2"})

    def test_revision_requires_new_warning_version(self) -> None:
        self._setup_and_start_stage_one()
        # 再登记一次同版本场景不存在；直接用无更新版本校验。
        self.service.revise_for_warning("planner", {
            "plan_id": "p1", "expected_revision": 1,
            "evacuation_start": "2026-09-26T08:30:00Z"})
        with self.assertRaises(InvalidState):
            self.service.revise_for_warning("planner", {
                "plan_id": "p1", "expected_revision": 2,
                "evacuation_start": "2026-09-26T08:31:00Z"})

    def test_revision_then_reconfirm_freezes_remaining_and_completes(self) -> None:
        self._setup_and_start_stage_one()
        self.service.revise_for_warning("planner", {
            "plan_id": "p1", "expected_revision": 1,
            "evacuation_start": "2026-09-26T08:30:00Z"})
        self.service.reconfirm_revision("director", {
            "plan_id": "p1", "candidate_id": "safety_first", "expected_revision": 2})
        self.service.record_receipt("dispatch", {
            "plan_id": "p1", "household_id": "h2", "event_type": "arrived",
            "observed_at": "2026-09-26T09:30:00Z", "headcount": 2, "idempotency_key": "h2-done"})
        explanation = self.service.explain_plan("audit", "p1")
        self.assertEqual(explanation["state"], "completed")
        self.assertEqual(explanation["people"]["actually_evacuated"], 5)
        self.assertEqual(explanation["warning"]["applied_version"], 2)

    def test_warning_revision_does_not_disturb_protected_terminal(self) -> None:
        self._setup_and_start_stage_one()
        self.service.revise_for_warning("planner", {
            "plan_id": "p1", "expected_revision": 1,
            "evacuation_start": "2026-09-26T08:30:00Z"})
        explanation = self.service.explain_plan("dispatch", "p1")
        self.assertEqual(explanation["people"]["actually_evacuated"], 3)


class ExplainTests(ServiceTestBase):
    def test_explain_reports_actual_numbers_reasons_and_villages(self) -> None:
        self.household(1, members=3)
        self.resources()
        self.plan()
        self.service.confirm_plan("director", {
            "plan_id": "p1", "candidate_id": "safety_first",
            "expected_revision": 1, "idempotency_key": "k"})
        explanation = self.service.explain_plan("audit", "p1")
        self.assertEqual(explanation["people"]["planned"], 3)
        self.assertEqual(explanation["people"]["actually_evacuated"], 0)
        self.assertEqual(explanation["unfinished"][0]["reason"], "not_started")
        self.assertEqual(explanation["village_impact"][0]["village_id"], "v1")
        self.assertIsNotNone(explanation["return_plan"])


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(EvacuationService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)

    def test_actor_required(self) -> None:
        response = self.app.handle(
            "POST", "/warnings", body=json.dumps(warning()).encode())
        self.assertEqual(response.status, 422)

    def test_full_flow_over_http(self) -> None:
        def call(method, path, payload=None, actor="planner"):
            body = json.dumps(payload).encode() if payload is not None else b""
            headers = {"X-Actor-Id": actor}
            return self.app.handle(method, path, headers, body)

        for uid, role in (("risk", "risk"), ("planner", "planner"), ("director", "director"),
                          ("dispatch", "dispatcher"), ("audit", "auditor")):
            self.assertEqual(call("POST", "/users", {
                "user_id": uid, "display_name": uid, "role": role}, actor=uid).status, 201)
        r = call("POST", "/warnings", warning(), actor="risk")
        self.assertEqual(r.status, 201)
        r = call("POST", "/villages", {"village_id": "v1", "name": "一村"})
        self.assertEqual(r.status, 201)
        r = call("POST", "/households", {
            "household_id": "h1", "village_id": "v1", "head_name": "户1",
            "members": 3, "dangerous_house": True})
        self.assertEqual(r.status, 201)
        self.assertEqual(call("POST", "/vehicles", {
            "vehicle_id": "b", "plate": "x", "seats": 10, "wheelchair_seats": 2}).status, 201)
        self.assertEqual(call("POST", "/shelters", {
            "shelter_id": "s", "name": "n", "beds_total": 20, "medical_beds_total": 4}).status, 201)
        self.assertEqual(call("POST", "/routes", {
            "route_id": "r", "village_id": "v1", "shelter_id": "s",
            "minutes": 30, "throughput_per_hour": 50}).status, 201)
        r = call("POST", "/plans", {"plan_id": "p", "warning_id": "w1",
                                    "evacuation_start": "2026-09-26T06:30:00Z"})
        self.assertEqual(r.status, 201)
        r = call("POST", "/plans/confirm", {
            "plan_id": "p", "candidate_id": "safety_first",
            "expected_revision": 1, "idempotency_key": "k"}, actor="director")
        self.assertEqual(r.status, 200)
        r = call("POST", "/receipts", {
            "plan_id": "p", "household_id": "h1", "event_type": "arrived",
            "observed_at": "2026-09-26T07:30:00Z", "headcount": 3,
            "idempotency_key": "r1"}, actor="dispatch")
        self.assertEqual(r.status, 201)
        r = call("GET", "/plans/p/explain", actor="audit")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body["state"], "completed")
        r = call("GET", "/audit/chain", actor="audit")
        self.assertEqual(r.body["valid"], True)


if __name__ == "__main__":
    unittest.main()
