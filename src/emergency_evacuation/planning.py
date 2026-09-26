"""确定性的分批撤离、车辆调度、床位匹配与约束校验。

规划层只接收普通字典，不触碰数据库，便于单元测试与重放。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping, Sequence

from .clock import parse_utc


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _add_minutes(text: str, minutes: int) -> str:
    moment = parse_utc(text)
    return (moment + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _add_hours(text: str, hours: int) -> str:
    return _add_minutes(text, hours * 60)


@dataclass(frozen=True, slots=True)
class HouseholdView:
    household_id: str
    village_id: str
    members: int
    vulnerable: tuple[Mapping[str, str], ...]
    transferable: bool
    transfer_kind: str
    needs_ambulance: bool
    shelter_required: bool
    family_assembly_point: str
    medical_destination_id: str

    @property
    def priority(self) -> int:
        """重点人群保护：特殊医疗 > 行动不便 > 在校儿童/孕幼 > 普通户。"""
        kinds = {item["kind"] for item in self.vulnerable}
        if "medical" in kinds:
            return 0
        if "mobility" in kinds:
            return 1
        if kinds & {"children", "infant"}:
            return 2
        return 3

    @property
    def mobility_count(self) -> int:
        return sum(1 for item in self.vulnerable if item["kind"] == "mobility")

    @property
    def medical_count(self) -> int:
        return sum(1 for item in self.vulnerable if item["kind"] == "medical")


def _household(row: Mapping[str, Any]) -> HouseholdView:
    return HouseholdView(
        household_id=row["household_id"],
        village_id=row["village_id"],
        members=int(row["members"]),
        vulnerable=tuple(row.get("vulnerable") or ()),
        transferable=bool(row["transferable"]),
        transfer_kind=row["transfer_kind"],
        needs_ambulance=bool(row["needs_ambulance"]),
        shelter_required=bool(row["shelter_required"]),
        family_assembly_point=row.get("family_assembly_point") or "",
        medical_destination_id=row.get("medical_destination_id") or "",
    )


def _ordered_households(rows: Sequence[Mapping[str, Any]]) -> list[HouseholdView]:
    views = [_household(row) for row in rows]
    return sorted(views, key=lambda item: (item.priority, item.village_id, item.household_id))


def _road_ok(village_id: str, roads: Sequence[Mapping[str, Any]]) -> bool:
    """村级道路约束：村内任一道路封闭则该车辆批次不可通行。"""
    for road in roads:
        if road["village_id"] == village_id and road["state"] == "closed":
            return False
    return True


def _road_detour(village_id: str, roads: Sequence[Mapping[str, Any]]) -> int:
    return max(
        (int(road["detour_minutes"]) for road in roads if road["village_id"] == village_id),
        default=0,
    )


def build_candidate(
    *,
    warning: Mapping[str, Any],
    households: Sequence[Mapping[str, Any]],
    vehicles: Sequence[Mapping[str, Any]],
    shelters: Sequence[Mapping[str, Any]],
    roads: Sequence[Mapping[str, Any]],
    batch_minutes: int = 90,
    return_window_hours: int = 72,
) -> dict[str, Any]:
    """生成单个确定性候选组合：分批撤离 + 车辆调度 + 临时床位。

    目标是所有危房户在承诺窗口（deadline_at）前完成转移；
    无法满足的家庭进入 uncovered，由约束检查解释原因。
    """
    deadline_at = warning["deadline_at"]
    warning_villages = set(warning["village_ids"])
    views = [item for item in _ordered_households(households) if item.village_id in warning_villages]

    ambulances = sorted(
        (v for v in vehicles if v.get("ambulance")),
        key=lambda item: (-int(item["seats"]), item["vehicle_id"]),
    )
    coaches = sorted(
        (v for v in vehicles if not v.get("ambulance")),
        key=lambda item: (-int(item["seats"]), item["vehicle_id"]),
    )
    ambulance_loads: dict[str, dict[str, Any]] = {
        v["vehicle_id"]: {"vehicle_id": v["vehicle_id"], "ambulance": True, "seats": int(v["seats"]),
                          "seats_for_mobility": int(v.get("seats_for_mobility", 0)),
                          "used_seats": 0, "used_mobility": 0, "villages": [], "household_ids": []}
        for v in ambulances
    }
    coach_loads: dict[str, dict[str, Any]] = {
        v["vehicle_id"]: {"vehicle_id": v["vehicle_id"], "ambulance": False, "seats": int(v["seats"]),
                          "seats_for_mobility": int(v.get("seats_for_mobility", 0)),
                          "used_seats": 0, "used_mobility": 0, "villages": [], "household_ids": []}
        for v in coaches
    }

    shelter_beds = {
        s["shelter_id"]: {
            "shelter_id": s["shelter_id"], "village_id": s["village_id"],
            "beds": int(s["beds"]), "used_beds": 0,
            "accessible_beds": int(s["accessible_beds"]), "used_accessible": 0,
            "medical_beds": int(s["medical_beds"]), "used_medical": 0,
        }
        for s in shelters
    }

    assignments: list[dict[str, Any]] = []
    uncovered: list[dict[str, str]] = []

    def _find_vehicle(loads: dict[str, dict[str, Any]], household: HouseholdView, need_mobility: bool) -> dict[str, Any] | None:
        for load in loads.values():
            seats_left = load["seats"] - load["used_seats"]
            mobility_left = load["seats_for_mobility"] - load["used_mobility"]
            if seats_left < household.members:
                continue
            if need_mobility and mobility_left < household.mobility_count:
                continue
            return load
        return None

    def _reserve_beds(household: HouseholdView) -> str | None:
        candidates = sorted(
            shelter_beds.values(),
            key=lambda bed: (bed["village_id"] != household.village_id, bed["shelter_id"]),
        )
        for bed in candidates:
            ordinary = household.members - household.mobility_count - household.medical_count
            if bed["used_beds"] + household.members > bed["beds"]:
                continue
            if bed["used_accessible"] + household.mobility_count > bed["accessible_beds"]:
                continue
            if bed["used_medical"] + household.medical_count > bed["medical_beds"]:
                continue
            bed["used_beds"] += household.members
            bed["used_accessible"] += household.mobility_count
            bed["used_medical"] += household.medical_count
            return str(bed["shelter_id"])
        return None

    mobile_households = [h for h in views if h.transfer_kind in {"vehicle", "ambulance"}]
    walking_households = [h for h in views if h.transfer_kind == "walk"]
    relative_households = [h for h in views if h.transfer_kind == "relative"]
    non_transferable = [h for h in views if not h.transferable]

    for household in non_transferable:
        uncovered.append({"household_id": household.household_id, "village_id": household.village_id,
                          "reason": "household_not_transferable"})

    def _assign(household: HouseholdView, load: dict[str, Any] | None, destination_kind: str,
                shelter_id: str | None) -> None:
        if load and household.village_id not in load["villages"]:
            load["villages"].append(household.village_id)
        assignments.append({
            "household_id": household.household_id,
            "village_id": household.village_id,
            "members": household.members,
            "priority_tier": household.priority,
            "vehicle_id": None if load is None else load["vehicle_id"],
            "destination_kind": destination_kind,
            "shelter_id": shelter_id,
            "family_assembly_point": household.family_assembly_point,
            "vulnerable": [dict(item) for item in household.vulnerable],
        })
        if load is not None:
            load["used_seats"] += household.members
            load["used_mobility"] += household.mobility_count
            if household.household_id not in load["household_ids"]:
                load["household_ids"].append(household.household_id)

    for household in mobile_households:
        if not _road_ok(household.village_id, roads):
            uncovered.append({"household_id": household.household_id, "village_id": household.village_id,
                              "reason": "road_closed"})
            continue
        need_ambulance = household.transfer_kind == "ambulance" or household.needs_ambulance
        pool = ambulance_loads if need_ambulance else coach_loads
        load = _find_vehicle(pool, household, need_mobility=household.mobility_count > 0)
        if load is None and need_ambulance:
            # 救护车不足时不允许降级为普通车辆（医疗保护约束）。
            uncovered.append({"household_id": household.household_id, "village_id": household.village_id,
                              "reason": "ambulance_capacity"})
            continue
        if load is None:
            uncovered.append({"household_id": household.household_id, "village_id": household.village_id,
                              "reason": "vehicle_capacity"})
            continue
        shelter_id: str | None = None
        medical_destination = household.medical_destination_id if need_ambulance and household.medical_destination_id else ""
        needs_shelter = household.shelter_required and not medical_destination
        if needs_shelter:
            shelter_id = _reserve_beds(household)
            if shelter_id is None:
                uncovered.append({"household_id": household.household_id, "village_id": household.village_id,
                                  "reason": "shelter_capacity"})
                continue
        _assign(household, load, "medical" if medical_destination else "shelter", shelter_id)

    for household in walking_households:
        # 步行户仍需确认村内道路未全断（封闭道路判定）。
        if not _road_ok(household.village_id, roads):
            uncovered.append({"household_id": household.household_id, "village_id": household.village_id,
                              "reason": "road_closed"})
            continue
        shelter_id = _reserve_beds(household) if household.shelter_required else None
        if household.shelter_required and shelter_id is None:
            uncovered.append({"household_id": household.household_id, "village_id": household.village_id,
                              "reason": "shelter_capacity"})
            continue
        _assign(household, None, "shelter", shelter_id)

    for household in relative_households:
        # 投亲靠友户使用家庭集合点，不占用床位。
        _assign(household, None, "relative", None)

    # 分批：按优先级分箱。批次 1 为医疗/行动不便，批次 2 为儿童孕幼，批次 3 为普通户。
    # 步行户与投亲户随其优先级同批自行前往。
    batch_definitions = (
        (1, "重点人群（特殊医疗/行动不便）", {0, 1}),
        (2, "在校儿童与孕幼", {2}),
        (3, "普通危房住户", {3}),
    )
    batches: list[dict[str, Any]] = []
    for batch_no, label, tiers in batch_definitions:
        rows = [item for item in assignments if item["priority_tier"] in tiers]
        if not rows:
            continue
        departs_at = _add_minutes(warning["issued_at"], (batch_no - 1) * batch_minutes)
        village_ids = sorted({row["village_id"] for row in rows})
        batches.append({
            "batch_no": batch_no,
            "label": label,
            "departs_at": departs_at,
            "arrive_by": _add_minutes(departs_at, 30 + max(
                (_road_detour(village_id, roads) for village_id in village_ids), default=0)),
            "village_ids": village_ids,
            "household_ids": [row["household_id"] for row in sorted(rows, key=lambda r: r["household_id"])],
            "persons": sum(int(row["members"]) for row in rows),
        })

    vehicle_plan = [
        {
            "vehicle_id": load["vehicle_id"],
            "ambulance": load["ambulance"],
            "seats": load["seats"],
            "used_seats": load["used_seats"],
            "seats_for_mobility": load["seats_for_mobility"],
            "used_mobility": load["used_mobility"],
            "village_ids": sorted(load["villages"]),
            "household_ids": load["household_ids"],
        }
        for load in sorted(list(ambulance_loads.values()) + list(coach_loads.values()),
                           key=lambda item: item["vehicle_id"])
        if load["used_seats"] > 0
    ]
    shelter_plan = [
        {
            "shelter_id": bed["shelter_id"],
            "village_id": bed["village_id"],
            "beds": bed["beds"],
            "reserved_beds": bed["used_beds"],
            "accessible_beds": bed["accessible_beds"],
            "reserved_accessible": bed["used_accessible"],
            "medical_beds": bed["medical_beds"],
            "reserved_medical": bed["used_medical"],
        }
        for bed in sorted(shelter_beds.values(), key=lambda item: item["shelter_id"])
        if bed["used_beds"] > 0
    ]

    protected = {
        "medical_persons": sum(item.medical_count for item in views),
        "mobility_persons": sum(item.mobility_count for item in views),
        "children_persons": sum(
            sum(1 for v in item.vulnerable if v["kind"] in {"children", "infant"}) for item in views
        ),
    }
    planned_persons = sum(item["members"] for item in assignments)
    candidate = {
        "assignments": sorted(assignments, key=lambda item: (item["priority_tier"], item["household_id"])),
        "batches": batches,
        "vehicles": vehicle_plan,
        "shelters": shelter_plan,
        "planned_households": len(assignments),
        "planned_persons": planned_persons,
        "uncovered": sorted(uncovered, key=lambda item: item["household_id"]),
        "protected": protected,
        "return_window": {
            "earliest_return_at": _add_hours(deadline_at, 12),
            "return_by_at": _add_hours(deadline_at, return_window_hours),
        },
    }
    candidate["constraints"] = evaluate_constraints(
        candidate=candidate, warning=warning, roads=roads, households=views,
    )
    return candidate


def evaluate_constraints(
    *,
    candidate: Mapping[str, Any],
    warning: Mapping[str, Any],
    roads: Sequence[Mapping[str, Any]],
    households: Sequence[HouseholdView],
) -> dict[str, Any]:
    """解释候选组合是否满足重点人群、道路通行与返迁窗口。"""
    violations: list[str] = []
    deadline_at = warning["deadline_at"]

    active_batches = [batch for batch in candidate["batches"] if not batch.get("retained")]
    latest_arrival = max((batch["arrive_by"] for batch in active_batches), default=None)
    if latest_arrival is not None and latest_arrival > deadline_at:
        violations.append("transfer_after_deadline")

    retained_ids = set(candidate.get("retained_household_ids", ()))
    by_id = {item.household_id: item for item in households}
    for assignment in candidate["assignments"]:
        if assignment["household_id"] in retained_ids:
            continue
        household = by_id[assignment["household_id"]]
        if household.transfer_kind == "ambulance" or household.needs_ambulance:
            if not assignment["vehicle_id"]:
                violations.append(f"medical_without_ambulance:{household.household_id}")
        if household.mobility_count and assignment["vehicle_id"] and not _vehicle_has_mobility(
                candidate["vehicles"], assignment["vehicle_id"], household.mobility_count):
            violations.append(f"mobility_seat:{household.household_id}")

    closed_villages = {road["village_id"] for road in roads if road["state"] == "closed"}
    batch_villages = {village for batch in active_batches for village in batch["village_ids"]}
    blocked = {item["village_id"] for item in candidate["uncovered"]
               if item["reason"] == "road_closed"}
    if closed_villages & (batch_villages | blocked):
        violations.append("batch_via_closed_road")

    window = candidate["return_window"]
    if window["return_by_at"] <= deadline_at:
        violations.append("return_window_before_deadline")

    uncovered = list(candidate["uncovered"])
    return {
        "feasible": not violations and not uncovered,
        "violations": violations,
        "uncovered_count": len(uncovered),
        "uncovered_reasons": _reason_counts(uncovered),
        "latest_arrive_by": latest_arrival,
        "deadline_at": deadline_at,
    }


def _vehicle_has_mobility(vehicles: Sequence[Mapping[str, Any]], vehicle_id: str, needed: int) -> bool:
    for vehicle in vehicles:
        if vehicle["vehicle_id"] == vehicle_id:
            return int(vehicle["seats_for_mobility"]) >= needed
    return False


def _reason_counts(uncovered: Sequence[Mapping[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in uncovered:
        counts[item["reason"]] = counts.get(item["reason"], 0) + 1
    return dict(sorted(counts.items()))


def safe_revise_plan(
    *,
    current_plan: Mapping[str, Any],
    latest_warning: Mapping[str, Any],
    households: Sequence[Mapping[str, Any]],
    vehicles: Sequence[Mapping[str, Any]],
    shelters: Sequence[Mapping[str, Any]],
    roads: Sequence[Mapping[str, Any]],
    executed_household_ids: set[str],
    batch_minutes: int = 90,
    return_window_hours: int = 72,
) -> dict[str, Any]:
    """预警修订：只重新规划尚未执行的阶段，已执行批次的资源占用予以保留。

    返回新的候选组合，以及保留/重排的家庭清单。
    """
    warning_villages = set(latest_warning["village_ids"])
    remaining_rows = [
        row for row in households
        if row["household_id"] not in executed_household_ids and row["village_id"] in warning_villages
    ]
    retained_assignments = [
        item for item in current_plan.get("assignments", [])
        if item["household_id"] in executed_household_ids
    ]

    # 保留已执行家庭对车辆与床位的占用，再对剩余资源做规划。
    used_vehicle_seats: dict[str, int] = {}
    used_vehicle_mobility: dict[str, int] = {}
    for assignment in retained_assignments:
        if assignment["vehicle_id"]:
            used_vehicle_seats[assignment["vehicle_id"]] = (
                used_vehicle_seats.get(assignment["vehicle_id"], 0) + int(assignment["members"]))
            used_vehicle_mobility[assignment["vehicle_id"]] = used_vehicle_mobility.get(
                assignment["vehicle_id"], 0) + sum(
                    1 for v in assignment["vulnerable"] if v["kind"] == "mobility")

    adjusted_vehicles: list[dict[str, Any]] = []
    for vehicle in vehicles:
        row = dict(vehicle)
        used = used_vehicle_seats.get(vehicle["vehicle_id"], 0)
        row["seats"] = max(0, int(vehicle["seats"]) - used)
        used_m = used_vehicle_mobility.get(vehicle["vehicle_id"], 0)
        row["seats_for_mobility"] = max(0, int(vehicle.get("seats_for_mobility", 0)) - used_m)
        adjusted_vehicles.append(row)

    adjusted_shelters: list[dict[str, Any]] = []
    reserved_beds: dict[str, int] = {}
    reserved_accessible: dict[str, int] = {}
    reserved_medical: dict[str, int] = {}
    for assignment in retained_assignments:
        if assignment["shelter_id"]:
            reserved_beds[assignment["shelter_id"]] = reserved_beds.get(
                assignment["shelter_id"], 0) + int(assignment["members"])
            reserved_accessible[assignment["shelter_id"]] = reserved_accessible.get(
                assignment["shelter_id"], 0) + sum(
                    1 for v in assignment["vulnerable"] if v["kind"] == "mobility")
            reserved_medical[assignment["shelter_id"]] = reserved_medical.get(
                assignment["shelter_id"], 0) + sum(
                    1 for v in assignment["vulnerable"] if v["kind"] == "medical")
    for shelter in shelters:
        row = dict(shelter)
        sid = shelter["shelter_id"]
        row["beds"] = max(0, int(shelter["beds"]) - reserved_beds.get(sid, 0))
        row["accessible_beds"] = max(0, int(shelter["accessible_beds"]) - reserved_accessible.get(sid, 0))
        row["medical_beds"] = max(0, int(shelter["medical_beds"]) - reserved_medical.get(sid, 0))
        adjusted_shelters.append(row)

    candidate = build_candidate(
        warning=latest_warning,
        households=remaining_rows,
        vehicles=adjusted_vehicles,
        shelters=adjusted_shelters,
        roads=roads,
        batch_minutes=batch_minutes,
        return_window_hours=return_window_hours,
    )
    # 保留只含已执行家庭的历史批次；部分执行的批次只保留已执行部分。
    retained_batches: list[dict[str, Any]] = []
    for batch in current_plan.get("batches", []):
        kept = [hid for hid in batch["household_ids"] if hid in executed_household_ids]
        if kept:
            retained_batches.append({**batch, "household_ids": kept,
                                     "persons": sum(
                                         _members(current_plan, hid) for hid in kept),
                                     "retained": True})
    offset = max((batch["batch_no"] for batch in retained_batches), default=0)
    revised_batches: list[dict[str, Any]] = []
    for batch in candidate["batches"]:
        shifted = dict(batch)
        shifted["batch_no"] = batch["batch_no"] + offset
        shifted["retained"] = False
        revised_batches.append(shifted)
    candidate["retained_household_ids"] = sorted(executed_household_ids)
    candidate["revised_household_ids"] = sorted(
        {row["household_id"] for row in remaining_rows})
    candidate["assignments"] = retained_assignments + candidate["assignments"]
    candidate["assignments"].sort(key=lambda item: (item["priority_tier"], item["household_id"]))
    candidate["batches"] = retained_batches + revised_batches
    candidate["planned_households"] = len(candidate["assignments"])
    candidate["planned_persons"] = sum(int(item["members"]) for item in candidate["assignments"])
    # 重新计算约束时使用完整的家庭视图。
    all_views = [_household(row) for row in households if row["village_id"] in warning_villages]
    candidate["constraints"] = evaluate_constraints(
        candidate=candidate, warning=latest_warning, roads=roads, households=all_views)
    return candidate


def _members(plan: Mapping[str, Any], household_id: str) -> int:
    for assignment in plan.get("assignments", []):
        if assignment["household_id"] == household_id:
            return int(assignment["members"])
    return 0
