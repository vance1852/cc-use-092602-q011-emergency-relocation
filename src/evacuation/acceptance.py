"""贯通预警版本、人口基线、候选组合、确认冻结、乱序回执与预警修订的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import EvacuationService


def _warning(version: int, level: str, issued_at: str, deadline: str, earliest_return: str) -> dict[str, object]:
    return {
        "warning_id": "w-geohaz-0926",
        "version": version,
        "issued_at": issued_at,
        "commit_deadline_at": deadline,
        "earliest_return_at": earliest_return,
        "level": level,
        "hazard": "局地强降雨诱发滑坡与泥石流",
        "affected_villages": ["v-north", "v-south"],
        "risk_curve": [
            {"seq": 1, "observed_at": issued_at, "risk_level": "yellow" if version == 1 else "orange", "note": "上游雨量站"},
            {"seq": 2, "observed_at": deadline[:11] + ("08:00:00Z" if version == 1 else "10:00:00Z"),
             "risk_level": level, "note": "风险升级"},
        ],
    }


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = EvacuationService(connection, FrozenClock(datetime(2026, 9, 26, 6, 0, tzinfo=timezone.utc)))

    for user_id, role in (
        ("risk", "risk"),
        ("planner", "planner"),
        ("director", "director"),
        ("dispatch", "dispatcher"),
        ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    service.issue_warning("risk", _warning(1, "orange", "2026-09-26T06:00:00Z", "2026-09-26T12:00:00Z", "2026-09-27T08:00:00Z"))

    service.register_village("planner", {"village_id": "v-north", "name": "北坡村", "assembly_point": "北坡小学操场", "contact": "村支书老王"})
    service.register_village("planner", {"village_id": "v-south", "name": "南沟村", "assembly_point": "南沟村委会", "contact": "村主任老李"})

    households = (
        {"household_id": "h-001", "village_id": "v-north", "head_name": "王贵山", "members": 3,
         "mobility_impaired": 1, "dangerous_house": True, "assembly_point": "北坡小学操场"},
        {"household_id": "h-002", "village_id": "v-north", "head_name": "王桂芳", "members": 2,
         "school_children": 1, "dangerous_house": True},
        {"household_id": "h-003", "village_id": "v-south", "head_name": "李长河", "members": 4,
         "special_medical": 1, "dangerous_house": True},
        {"household_id": "h-004", "village_id": "v-south", "head_name": "李自立", "members": 2,
         "transport_required": False},
    )
    for household in households:
        service.upsert_household("planner", household)

    service.register_vehicle("planner", {"vehicle_id": "bus-1", "plate": "晋A·0001", "seats": 10, "wheelchair_seats": 2})
    service.register_vehicle("planner", {"vehicle_id": "van-1", "plate": "晋A·0002", "seats": 6, "wheelchair_seats": 1})
    service.register_shelter("planner", {"shelter_id": "sh-center", "name": "镇中心安置点", "address": "镇文体中心", "beds_total": 30, "medical_beds_total": 4})
    service.register_route("planner", {"route_id": "r-north", "village_id": "v-north", "shelter_id": "sh-center", "minutes": 30, "throughput_per_hour": 50})
    service.register_route("planner", {"route_id": "r-south", "village_id": "v-south", "shelter_id": "sh-center", "minutes": 40, "throughput_per_hour": 40})

    plan = service.generate_plan("planner", {
        "plan_id": "plan-0926",
        "warning_id": "w-geohaz-0926",
        "evacuation_start": "2026-09-26T06:30:00Z",
    })
    feasible = [c["candidate_id"] for c in plan["candidates"] if c["feasible"]]
    confirmed = service.confirm_plan("director", {
        "plan_id": "plan-0926",
        "candidate_id": "safety_first",
        "expected_revision": 1,
        "idempotency_key": "confirm-0926-1",
    })
    # 幂等重放确认请求。
    replayed = service.confirm_plan("director", {
        "plan_id": "plan-0926",
        "candidate_id": "safety_first",
        "expected_revision": 1,
        "idempotency_key": "confirm-0926-1",
    })

    # 第一批现场回执：通知、出发、安全到达；其中混入重复与乱序回执。
    receipts = (
        {"event_type": "notified", "observed_at": "2026-09-26T07:05:00Z"},
        {"event_type": "departed", "observed_at": "2026-09-26T07:20:00Z"},
        {"event_type": "arrived", "observed_at": "2026-09-26T07:50:00Z", "headcount": 3},
        {"event_type": "arrived", "observed_at": "2026-09-26T07:50:00Z", "headcount": 3},  # 重复
        {"event_type": "departed", "observed_at": "2026-09-26T07:10:00Z"},  # 乱序旧事件
    )
    receipt_results = []
    for index, receipt in enumerate(receipts, start=1):
        receipt_results.append(service.record_receipt("dispatch", {
            "plan_id": "plan-0926",
            "household_id": "h-001",
            "idempotency_key": f"rcpt-h001-{index}",
            "reporter": "北坡网格员",
            **receipt,
        }))

    # 预警升级为红色并延长承诺窗口，触发方案修订。
    service.issue_warning("risk", _warning(2, "red", "2026-09-26T08:30:00Z", "2026-09-26T14:00:00Z", "2026-09-27T12:00:00Z"))
    revised = service.revise_for_warning("planner", {
        "plan_id": "plan-0926",
        "expected_revision": 1,
        "evacuation_start": "2026-09-26T08:30:00Z",
    })
    revision_feasible = [c["candidate_id"] for c in revised["candidates"] if c["feasible"]]
    service.reconfirm_revision("director", {
        "plan_id": "plan-0926",
        "candidate_id": "safety_first",
        "expected_revision": 2,
    })

    # 其余家庭完成转移，h-003 中 1 人特殊医疗豁免就近投亲；随后 h-001 在返迁窗口后返迁。
    tail = (
        ("h-002", "arrived", "2026-09-26T09:10:00Z", 2),
        ("h-003", "exempt", "2026-09-26T09:00:00Z", 4),
        ("h-004", "arrived", "2026-09-26T09:30:00Z", 2),
        ("h-001", "returned", "2026-09-27T12:30:00Z", 3),
    )
    for index, (household_id, event_type, observed_at, headcount) in enumerate(tail, start=10):
        service.record_receipt("dispatch", {
            "plan_id": "plan-0926",
            "household_id": household_id,
            "event_type": event_type,
            "observed_at": observed_at,
            "headcount": headcount,
            "idempotency_key": f"rcpt-{household_id}-{index}",
        })

    explanation = service.explain_plan("audit", "plan-0926")
    audit = service.audit_chain("audit")
    connection.close()
    return {
        "status": "ok",
        "workspace": workspace.name,
        "initial_feasible_candidates": feasible,
        "confirm_replayed": replayed == confirmed,
        "receipt_merge": {
            "duplicates_marked": [item["duplicate"] for item in receipt_results],
            "applied": [item["applied"] for item in receipt_results],
            "final_state": receipt_results[-1]["current_state"],
        },
        "revision_feasible_candidates": revision_feasible,
        "final_plan_state": explanation["state"],
        "people": explanation["people"],
        "village_impact": explanation["village_impact"],
        "unfinished": explanation["unfinished"],
        "return_plan_stages": len(explanation["return_plan"]["stages"]),
        "returned_actual": explanation["returned_actual"],
        "audit": audit,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行应急转移协同服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
