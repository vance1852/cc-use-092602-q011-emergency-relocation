"""贯通预警版本、候选组合、确认冻结、现场回执、修订与返迁的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import EvacuationService


def _curve(*scores: tuple[str, float]) -> list[dict[str, object]]:
    return [{"observed_at": at, "risk_score": score} for at, score in scores]


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = EvacuationService(connection, FrozenClock(
        datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc)))
    for user_id, role in (
        ("plan", "planner"), ("dispatch", "dispatcher"), ("chief", "commander"),
        ("field", "field"), ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    # 村级人口基线与家庭可转移属性。
    service.register_village("plan", {"village_id": "qingshan", "name": "青山村",
                                      "households": 86, "population": 312, "at_risk_households": 3})
    service.register_village("plan", {"village_id": "baishi", "name": "白石村",
                                      "households": 64, "population": 233, "at_risk_households": 2})
    service.upsert_household("plan", {
        "household_id": "h-med", "village_id": "qingshan", "members": 2,
        "vulnerable": [{"kind": "medical", "note": "隔日透析"}],
        "transfer_kind": "ambulance", "medical_destination_id": "clinic-1",
        "family_assembly_point": "村卫生室门口"})
    service.upsert_household("plan", {
        "household_id": "h-mob", "village_id": "qingshan", "members": 2,
        "vulnerable": [{"kind": "mobility", "note": "偏瘫老人"}],
        "family_assembly_point": "老樟树下"})
    service.upsert_household("plan", {
        "household_id": "h-kid", "village_id": "baishi", "members": 3,
        "vulnerable": [{"kind": "children", "note": "村小三年级"},
                       {"kind": "children", "note": "学前班"}],
        "family_assembly_point": "白石小学操场"})
    service.upsert_household("plan", {
        "household_id": "h-reg", "village_id": "baishi", "members": 4,
        "family_assembly_point": "白石小学操场"})
    service.upsert_household("plan", {
        "household_id": "h-rel", "village_id": "qingshan", "members": 2,
        "transfer_kind": "relative", "shelter_required": False,
        "family_assembly_point": "亲戚自驾在镇口接应"})

    # 车辆、临时床位和道路通行。
    service.register_vehicle("dispatch", {"vehicle_id": "bus-1", "kind": "中巴",
                                          "seats": 20, "seats_for_mobility": 2})
    service.register_vehicle("dispatch", {"vehicle_id": "amb-1", "kind": "救护车",
                                          "seats": 4, "ambulance": True,
                                          "seats_for_mobility": 2})
    service.register_shelter("dispatch", {"shelter_id": "shelter-qs", "name": "青山村礼堂",
                                          "village_id": "qingshan", "beds": 10,
                                          "accessible_beds": 2, "medical_beds": 2})
    service.register_shelter("dispatch", {"shelter_id": "shelter-bs", "name": "白石小学体育馆",
                                          "village_id": "baishi", "beds": 10})
    service.update_road("dispatch", {"road_id": "road-qingshan", "village_id": "qingshan"})
    service.update_road("dispatch", {"road_id": "road-baishi", "village_id": "baishi",
                                     "state": "restricted", "detour_minutes": 20})

    # 预警版本一与风险变化曲线。
    warning_one = {
        "warning_id": "w-flood-0926", "revision": 1, "level": "orange",
        "issued_at": "2026-09-26T02:00:00Z", "deadline_at": "2026-09-26T12:00:00Z",
        "village_ids": ["qingshan", "baishi"],
        "risk_curve": _curve(("2026-09-26T01:00:00Z", 42), ("2026-09-26T01:30:00Z", 58),
                             ("2026-09-26T02:00:00Z", 67)),
        "notes": "持续强降雨，滑坡风险上升"}
    service.record_warning("plan", warning_one)
    first = service.generate_plan("dispatch", "w-flood-0926", None)
    plan_one_id = first["plan_id"]
    assert first["candidate"]["constraints"]["feasible"]
    confirmed = service.confirm_plan("chief", plan_one_id, 1)
    frozen = service.freeze_plan("chief", plan_one_id, confirmed["revision"])

    # 现场回执：正常、重复、乱序。
    service.record_receipt("field", {"plan_id": plan_one_id, "receipt_id": "rc-1",
                                     "household_id": "h-med", "event_type": "departed",
                                     "event_at": "2026-09-26T02:05:00Z", "persons": 2,
                                     "vehicle_id": "amb-1"})
    duplicate = service.record_receipt("field", {"plan_id": plan_one_id, "receipt_id": "rc-1",
                                                 "household_id": "h-med", "event_type": "departed",
                                                 "event_at": "2026-09-26T02:05:00Z", "persons": 2,
                                                 "vehicle_id": "amb-1"})
    out_of_order = service.record_receipt("field", {"plan_id": plan_one_id, "receipt_id": "rc-2",
                                                    "household_id": "h-med", "event_type": "notified",
                                                    "event_at": "2026-09-26T01:50:00Z", "persons": 2})
    arrived = service.record_receipt("field", {"plan_id": plan_one_id, "receipt_id": "rc-3",
                                               "household_id": "h-med", "event_type": "arrived",
                                               "event_at": "2026-09-26T02:40:00Z", "persons": 2,
                                               "shelter_id": "clinic-1"})

    # 预警修订：窗口延长到 18:00；只重排尚未执行的阶段。
    warning_two = dict(warning_one, revision=2, level="red",
                       deadline_at="2026-09-26T18:00:00Z",
                       risk_curve=_curve(("2026-09-26T01:00:00Z", 42),
                                         ("2026-09-26T02:00:00Z", 67),
                                         ("2026-09-26T03:00:00Z", 79)),
                       notes="上游水位继续上涨，转移窗口延长")
    service.record_warning("plan", warning_two)
    revised = service.generate_plan("dispatch", "w-flood-0926", None)
    plan_two_id = revised["plan_id"]
    assert revised["based_on_plan_id"] == plan_one_id
    assert "h-med" in revised["candidate"]["retained_household_ids"]
    confirmed_two = service.confirm_plan("chief", plan_two_id, 1)
    frozen_two = service.freeze_plan("chief", plan_two_id, confirmed_two["revision"])

    # 剩余批次回执：拒迁后经劝导重新出发（失败终态可被更晚正面事件推进）。
    for receipt_id, household_id, event_type, event_at, persons, extra in (
        ("rc-4", "h-mob", "departed", "2026-09-26T03:35:00Z", 2, {"vehicle_id": "bus-1"}),
        ("rc-5", "h-mob", "sheltered", "2026-09-26T04:10:00Z", 2, {"shelter_id": "shelter-qs"}),
        ("rc-6", "h-kid", "sheltered", "2026-09-26T04:15:00Z", 3, {"shelter_id": "shelter-bs"}),
        ("rc-7", "h-reg", "sheltered", "2026-09-26T04:20:00Z", 4, {"shelter_id": "shelter-bs"}),
        ("rc-8", "h-rel", "refused", "2026-09-26T04:30:00Z", 2, {"note": "坚持投亲但道路受阻"}),
    ):
        result = service.record_receipt("field", {"plan_id": plan_two_id, "receipt_id": receipt_id,
                                                  "household_id": household_id,
                                                  "event_type": event_type, "event_at": event_at,
                                                  "persons": persons, **extra})
        assert result["applied"], (receipt_id, result)
    recovered = service.record_receipt("field", {"plan_id": plan_two_id, "receipt_id": "rc-9",
                                                 "household_id": "h-rel", "event_type": "departed",
                                                 "event_at": "2026-09-26T05:00:00Z", "persons": 2})
    # 已返迁是成功性终态：之后到达的非返迁事件一律不得覆盖。
    service.record_receipt("field", {"plan_id": plan_two_id, "receipt_id": "rc-10",
                                     "household_id": "h-med", "event_type": "returned",
                                     "event_at": "2026-09-27T08:00:00Z", "persons": 2})
    protected = service.record_receipt("field", {"plan_id": plan_two_id, "receipt_id": "rc-11",
                                                 "household_id": "h-med", "event_type": "arrived",
                                                 "event_at": "2026-09-27T09:00:00Z", "persons": 2})

    service.register_return_plan("chief", plan_two_id, None)
    status = service.plan_status("chief", plan_two_id)
    curve = service.warning_curve("w-flood-0926")
    audit = service.audit_chain("audit")
    connection.close()
    return {
        "status": "ok",
        "workspace": workspace.name,
        "plan_one": plan_one_id,
        "frozen_one": frozen,
        "receipt_merge": {"duplicate": duplicate["reason"], "out_of_order": out_of_order["reason"],
                          "arrived": arrived["state"], "terminal_protected": protected["reason"]},
        "plan_two": plan_two_id,
        "frozen_two": frozen_two,
        "retained": revised["candidate"]["retained_household_ids"],
        "warning_trend": curve["revisions"][-1]["trend"],
        "actual_evacuated_persons": status["actual_evacuated_persons"],
        "sheltered_persons": status["sheltered_persons"],
        "unfinished": status["unfinished"],
        "villages": status["villages"],
        "return_plan": status["return_plan"],
        "audit": audit,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行汛期应急转移协同服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
