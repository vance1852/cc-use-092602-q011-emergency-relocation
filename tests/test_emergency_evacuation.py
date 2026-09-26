from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from emergency_evacuation.api import JsonApplication
from emergency_evacuation.clock import FrozenClock
from emergency_evacuation.errors import Conflict, Forbidden, InvalidState
from emergency_evacuation.planning import build_candidate, safe_revise_plan
from emergency_evacuation.service import EvacuationService


WARNING = {
    "warning_id": "w1", "revision": 1, "level": "orange",
    "issued_at": "2026-09-26T02:00:00Z", "deadline_at": "2026-09-26T12:00:00Z",
    "village_ids": ["v1"],
    "risk_curve": [{"observed_at": "2026-09-26T01:00:00Z", "risk_score": 40},
                   {"observed_at": "2026-09-26T02:00:00Z", "risk_score": 66}],
}


def household(hid: str, members: int = 3, **kwargs) -> dict:
    row = {"household_id": hid, "village_id": "v1", "members": members,
           "vulnerable": [], "transferable": True, "transfer_kind": "vehicle",
           "needs_ambulance": False, "shelter_required": True,
           "family_assembly_point": "操场", "medical_destination_id": ""}
    row.update(kwargs)
    return row


class PlanningTests(unittest.TestCase):
    def test_priority_batches_protect_vulnerable_first(self) -> None:
        rows = [
            household("h-normal"),
            household("h-med", 2, transfer_kind="ambulance",
                      vulnerable=[{"kind": "medical", "note": "透析"}],
                      needs_ambulance=True, medical_destination_id="clinic"),
            household("h-mob", 2, vulnerable=[{"kind": "mobility", "note": "轮椅"}]),
            household("h-kid", 3, vulnerable=[{"kind": "children", "note": "小学"}]),
        ]
        vehicles = [
            {"vehicle_id": "bus", "kind": "bus", "seats": 20, "ambulance": False,
             "seats_for_mobility": 2, "active": 1},
            {"vehicle_id": "amb", "kind": "ambulance", "seats": 3, "ambulance": True,
             "seats_for_mobility": 1, "active": 1},
        ]
        shelters = [{"shelter_id": "s1", "name": "礼堂", "village_id": "v1", "beds": 20,
                     "accessible_beds": 4, "medical_beds": 2, "active": 1}]
        candidate = build_candidate(warning=WARNING, households=rows, vehicles=vehicles,
                                    shelters=shelters, roads=[])
        self.assertTrue(candidate["constraints"]["feasible"], candidate["constraints"])
        self.assertEqual([b["batch_no"] for b in candidate["batches"]], [1, 2, 3])
        self.assertIn("h-med", candidate["batches"][0]["household_ids"])
        self.assertIn("h-mob", candidate["batches"][0]["household_ids"])
        self.assertEqual(candidate["batches"][2]["household_ids"], ["h-normal"])
        amb_plan = next(v for v in candidate["vehicles"] if v["vehicle_id"] == "amb")
        self.assertEqual(amb_plan["used_seats"], 2)
        self.assertEqual(candidate["protected"]["medical_persons"], 1)

    def test_medical_household_never_downgraded_to_bus(self) -> None:
        rows = [household("h-med", 2, transfer_kind="ambulance",
                          vulnerable=[{"kind": "medical", "note": ""}], needs_ambulance=True)]
        vehicles = [{"vehicle_id": "bus", "kind": "bus", "seats": 20, "ambulance": False,
                     "seats_for_mobility": 0, "active": 1}]
        candidate = build_candidate(warning=WARNING, households=rows, vehicles=vehicles,
                                    shelters=[], roads=[])
        self.assertFalse(candidate["constraints"]["feasible"])
        self.assertEqual(candidate["uncovered"][0]["reason"], "ambulance_capacity")

    def test_closed_road_and_short_window_are_violations(self) -> None:
        rows = [household("h1")]
        vehicles = [{"vehicle_id": "bus", "seats": 20, "ambulance": False,
                     "seats_for_mobility": 0, "active": 1}]
        shelters = [{"shelter_id": "s1", "name": "礼堂", "village_id": "v1", "beds": 20,
                     "accessible_beds": 0, "medical_beds": 0, "active": 1}]
        roads = [{"road_id": "r1", "village_id": "v1", "state": "closed", "detour_minutes": 0}]
        candidate = build_candidate(warning=WARNING, households=rows, vehicles=vehicles,
                                    shelters=shelters, roads=roads)
        self.assertIn("batch_via_closed_road", candidate["constraints"]["violations"])
        self.assertEqual(candidate["uncovered"][0]["reason"], "road_closed")

        short = dict(WARNING, deadline_at="2026-09-26T02:20:00Z")
        late = build_candidate(warning=short, households=rows, vehicles=vehicles,
                               shelters=shelters, roads=[])
        self.assertIn("transfer_after_deadline", late["constraints"]["violations"])

    def test_shelter_capacity_shortfall_is_explained(self) -> None:
        rows = [household("h1", 10)]
        vehicles = [{"vehicle_id": "bus", "seats": 20, "ambulance": False,
                     "seats_for_mobility": 0, "active": 1}]
        shelters = [{"shelter_id": "s1", "name": "礼堂", "village_id": "v1", "beds": 4,
                     "accessible_beds": 0, "medical_beds": 0, "active": 1}]
        candidate = build_candidate(warning=WARNING, households=rows, vehicles=vehicles,
                                    shelters=shelters, roads=[])
        self.assertFalse(candidate["constraints"]["feasible"])
        self.assertEqual(candidate["constraints"]["uncovered_reasons"],
                         {"shelter_capacity": 1})

    def test_safe_revision_only_replans_unexecuted_stage(self) -> None:
        rows = [household("h-a", 2, vulnerable=[{"kind": "medical", "note": ""}],
                          transfer_kind="ambulance", needs_ambulance=True,
                          medical_destination_id="clinic"),
                household("h-b", 3)]
        vehicles = [
            {"vehicle_id": "amb", "seats": 3, "ambulance": True, "seats_for_mobility": 1,
             "active": 1},
            {"vehicle_id": "bus", "seats": 10, "ambulance": False, "seats_for_mobility": 0,
             "active": 1},
        ]
        shelters = [{"shelter_id": "s1", "name": "礼堂", "village_id": "v1", "beds": 20,
                     "accessible_beds": 2, "medical_beds": 2, "active": 1}]
        first = build_candidate(warning=WARNING, households=rows, vehicles=vehicles,
                                shelters=shelters, roads=[])
        revised_warning = dict(WARNING, deadline_at="2026-09-26T18:00:00Z")
        second = safe_revise_plan(current_plan=first, latest_warning=revised_warning,
                                  households=rows, vehicles=vehicles, shelters=shelters,
                                  roads=[], executed_household_ids={"h-a"})
        self.assertEqual(second["retained_household_ids"], ["h-a"])
        self.assertEqual(second["revised_household_ids"], ["h-b"])
        retained_batch = next(b for b in second["batches"] if b.get("retained"))
        self.assertEqual(retained_batch["household_ids"], ["h-a"])
        # 救护车座位已被执行户占用，剩余规划里不应再出现救护车派车。
        self.assertTrue(all(v["vehicle_id"] != "amb" for v in second["vehicles"]))


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc))
        self.service = EvacuationService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"), ("dispatch", "dispatcher"), ("chief", "commander"),
            ("field", "field"), ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.register_village("plan", {"village_id": "v1", "name": "青山村",
                                               "households": 50, "population": 180,
                                               "at_risk_households": 2})
        self.service.register_vehicle("dispatch", {"vehicle_id": "bus", "kind": "中巴",
                                                   "seats": 20, "seats_for_mobility": 2})
        self.service.register_shelter("dispatch", {"shelter_id": "s1", "name": "礼堂",
                                                   "village_id": "v1", "beds": 20,
                                                   "accessible_beds": 2, "medical_beds": 2})

    def tearDown(self) -> None:
        self.connection.close()

    def _warning(self, revision: int = 1, **overrides) -> dict:
        warning = {
            "warning_id": "w1", "revision": revision, "level": "orange",
            "issued_at": "2026-09-26T02:00:00Z", "deadline_at": "2026-09-26T12:00:00Z",
            "village_ids": ["v1"],
            "risk_curve": [{"observed_at": "2026-09-26T01:00:00Z", "risk_score": 40},
                           {"observed_at": "2026-09-26T02:00:00Z", "risk_score": 66}],
        }
        warning.update(overrides)
        return warning

    def _ready_plan(self, warning_id: str = "w1") -> str:
        self.service.upsert_household("plan", {
            "household_id": "h1", "village_id": "v1", "members": 4,
            "family_assembly_point": "操场"})
        result = self.service.generate_plan("dispatch", warning_id, None)
        plan_id = result["plan_id"]
        self.service.confirm_plan("chief", plan_id, 1)
        self.service.freeze_plan("chief", plan_id, 2)
        return plan_id

    def test_warning_revision_must_be_sequential(self) -> None:
        self.service.record_warning("plan", self._warning(1))
        with self.assertRaises(Conflict):
            self.service.record_warning("plan", self._warning(3))

    def test_risk_curve_must_be_chronological(self) -> None:
        with self.assertRaises(Exception):
            self.service.record_warning("plan", self._warning(
                risk_curve=[{"observed_at": "2026-09-26T03:00:00Z", "risk_score": 70},
                            {"observed_at": "2026-09-26T02:00:00Z", "risk_score": 60}]))

    def test_plan_generation_is_deterministic_and_replays(self) -> None:
        self.service.upsert_household("plan", {"household_id": "h1", "village_id": "v1",
                                               "members": 4, "family_assembly_point": "操场"})
        self.service.record_warning("plan", self._warning(1))
        first = self.service.generate_plan("dispatch", "w1", None)
        second = self.service.generate_plan("dispatch", "w1", None)
        self.assertEqual(first["plan_id"], second["plan_id"])
        self.assertTrue(second["replayed"])

    def test_regenerate_after_input_change_cancels_and_then_revives(self) -> None:
        self.service.record_warning("plan", self._warning(1))
        self.service.upsert_household("plan", {"household_id": "h1", "village_id": "v1",
                                               "members": 4, "family_assembly_point": "操场"})
        first = self.service.generate_plan("dispatch", "w1", {"batch_minutes": 60})
        second = self.service.generate_plan("dispatch", "w1", {"batch_minutes": 120})
        self.assertNotEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM evacuation_plans WHERE plan_id=?",
                (first["plan_id"],)).fetchone()["state"], "cancelled")
        # 再次变更参数会作废第二个候选。
        third = self.service.generate_plan("dispatch", "w1", {"batch_minutes": 180})
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM evacuation_plans WHERE plan_id=?",
                (second["plan_id"],)).fetchone()["state"], "cancelled")
        # 回到第二组输入：复活已作废的候选行而非唯一约束冲突。
        revived = self.service.generate_plan("dispatch", "w1", {"batch_minutes": 120})
        self.assertFalse(revived["replayed"])
        self.assertEqual(revived["plan_id"], second["plan_id"])
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM evacuation_plans WHERE plan_id=?",
                (second["plan_id"],)).fetchone()["state"], "proposed")
        del third

    def test_infeasible_plan_cannot_freeze(self) -> None:
        self.service.record_warning("plan", self._warning(
            1, deadline_at="2026-09-26T02:20:00Z"))
        self.service.upsert_household("plan", {"household_id": "h1", "village_id": "v1",
                                               "members": 4, "family_assembly_point": "操场"})
        plan_id = self.service.generate_plan("dispatch", "w1", None)["plan_id"]
        self.service.confirm_plan("chief", plan_id, 1)
        with self.assertRaises(InvalidState):
            self.service.freeze_plan("chief", plan_id, 2)

    def test_roles_are_enforced(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.record_warning("dispatch", self._warning(1))
        with self.assertRaises(Forbidden):
            self.service.freeze_plan("plan", "x", 1)

    def test_receipts_deduplicate_out_of_order_and_protect_terminal(self) -> None:
        self.service.record_warning("plan", self._warning(1))
        plan_id = self._ready_plan()
        base = {"plan_id": plan_id, "household_id": "h1", "persons": 4}
        departed = {"receipt_id": "r1", "event_type": "departed",
                    "event_at": "2026-09-26T03:00:00Z"}
        self.assertTrue(self.service.record_receipt("field", {**base, **departed})["applied"])
        duplicate = self.service.record_receipt("field", {**base, **departed})
        self.assertFalse(duplicate["applied"])
        self.assertEqual(duplicate["reason"], "duplicate")
        early = self.service.record_receipt(
            "field", {**base, "receipt_id": "r2", "event_type": "notified",
                      "event_at": "2026-09-26T02:30:00Z"})
        self.assertEqual(early["reason"], "out_of_order")
        # 失联终态。
        absent = self.service.record_receipt(
            "field", {**base, "receipt_id": "r3", "event_type": "absent",
                      "event_at": "2026-09-26T05:00:00Z", "note": "上门无人"})
        self.assertTrue(absent["applied"])
        # 失败性终态可被更晚的正面事件推进（找到人后撤离）。
        recovered = self.service.record_receipt(
            "field", {**base, "receipt_id": "r4", "event_type": "departed",
                      "event_at": "2026-09-26T06:00:00Z"})
        self.assertTrue(recovered["applied"])
        # 返迁成功终态之后，晚到的非返迁事件不得覆盖。
        self.service.record_receipt(
            "field", {**base, "receipt_id": "r5", "event_type": "returned",
                      "event_at": "2026-09-28T08:00:00Z"})
        blocked = self.service.record_receipt(
            "field", {**base, "receipt_id": "r6", "event_type": "arrived",
                      "event_at": "2026-09-28T09:00:00Z"})
        self.assertEqual(blocked["reason"], "terminal_protected")
        status = self.service.plan_status("chief", plan_id)
        self.assertEqual(status["unfinished"], [])

    def test_warning_revision_keeps_executed_stage(self) -> None:
        self.service.record_warning("plan", self._warning(1))
        self.service.upsert_household("plan", {"household_id": "h1", "village_id": "v1",
                                               "members": 4, "family_assembly_point": "操场"})
        self.service.upsert_household("plan", {"household_id": "h2", "village_id": "v1",
                                               "members": 2, "family_assembly_point": "操场"})
        first_id = self._ready_plan()
        self.service.record_receipt("field", {
            "plan_id": first_id, "receipt_id": "r1", "household_id": "h1",
            "event_type": "sheltered", "event_at": "2026-09-26T04:00:00Z",
            "persons": 4, "shelter_id": "s1"})
        self.service.record_warning("plan", self._warning(
            2, level="red", deadline_at="2026-09-26T18:00:00Z",
            risk_curve=[{"observed_at": "2026-09-26T01:00:00Z", "risk_score": 40},
                        {"observed_at": "2026-09-26T03:00:00Z", "risk_score": 82}]))
        revised = self.service.generate_plan("dispatch", "w1", None)
        self.assertEqual(revised["based_on_plan_id"], first_id)
        self.assertEqual(revised["candidate"]["retained_household_ids"], ["h1"])
        self.assertEqual(revised["candidate"]["revised_household_ids"], ["h2"])
        second_id = revised["plan_id"]
        self.service.confirm_plan("chief", second_id, 1)
        self.service.freeze_plan("chief", second_id, 2)
        self.service.record_receipt("field", {
            "plan_id": second_id, "receipt_id": "r2", "household_id": "h2",
            "event_type": "sheltered", "event_at": "2026-09-26T05:00:00Z",
            "persons": 2, "shelter_id": "s1"})
        # 对已保留（执行）户在新计划上登记更晚返迁，必须真实落库并沿链合并。
        returned = self.service.record_receipt("field", {
            "plan_id": second_id, "receipt_id": "r3", "household_id": "h1",
            "event_type": "returned", "event_at": "2026-09-28T08:00:00Z",
            "persons": 4})
        self.assertTrue(returned["applied"])
        local = self.connection.execute(
            "SELECT state FROM household_progress WHERE plan_id=? AND household_id='h1'",
            (second_id,)).fetchone()
        self.assertEqual(local["state"], "returned")
        status = self.service.plan_status("chief", second_id)
        self.assertEqual(status["actual_evacuated_persons"], 6)
        self.assertEqual(status["returned_persons"], 4)
        self.assertEqual(status["unfinished"], [])
        self.assertEqual(status["villages"][0]["actual_evacuated_persons"], 6)
        self.assertEqual(status["return_plan"]["return_by_at"], "2026-09-29T18:00:00Z")

    def test_status_explains_unfinished_and_village_impact(self) -> None:
        self.service.record_warning("plan", self._warning(1))
        plan_id = self._ready_plan()
        status = self.service.plan_status("chief", plan_id)
        self.assertEqual(status["planned_persons"], 4)
        self.assertEqual(status["actual_evacuated_persons"], 0)
        self.assertEqual(status["unfinished"][0]["reason"], "not_started")
        self.assertEqual(status["villages"][0]["village_id"], "v1")
        self.assertEqual(status["villages"][0]["at_risk_households"], 2)

    def test_audit_chain_detects_tampering(self) -> None:
        self.service.record_warning("plan", self._warning(1))
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE evac_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(EvacuationService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def test_health_and_actor_header(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        response = self.app.handle("GET", "/plans/none/status")
        self.assertEqual(response.status, 422)

    def test_validation_error_shape(self) -> None:
        response = self.app.handle("POST", "/warnings", body=b"not-json",
                                   headers={"X-Actor-Id": "plan"})
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")


if __name__ == "__main__":
    unittest.main()
