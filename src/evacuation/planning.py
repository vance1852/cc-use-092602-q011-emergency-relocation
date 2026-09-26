"""确定性的分批撤离、车辆调度、临时床位与返迁规划。

规划器是纯函数：同样的输入必然得到同样顺序的候选组合，便于在确认前
重复推演、在确认时复核资源是否被其他方案占用。
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta
from typing import Any, Mapping, Sequence

from .clock import parse_utc, utc_text


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _dt(value: str):
    return parse_utc(value, "时间")


def _iso(value) -> str:
    return utc_text(value)


ROUTE_FACTOR = {"open": 1.0, "restricted": 0.5, "closed": 0.0}
STAGE_PRIORITY = ("vulnerable_first", "fast_clearance", "shelter_balanced")


def route_state_at(events: Sequence[Mapping[str, Any]], at_text: str) -> str:
    """按生效时间归并道路状态；同一时刻以事件号大者为准，默认 open。"""
    at = _dt(at_text)
    state = "open"
    for event in sorted(events, key=lambda item: (item["effective_at"], int(item["event_id"]))):
        if _dt(event["effective_at"]) <= at:
            state = event["state"]
    return state


def _active_external_windows(windows: Sequence[Mapping[str, Any]], start: str, end: str):
    return [w for w in windows if _dt(w["window_start"]) < _dt(end) and _dt(w["window_end"]) > _dt(start)]


def _overlaps(start_a, end_a, start_b, end_b) -> bool:
    return start_a < end_b and start_b < end_a


def generate_candidates(
    warning: Mapping[str, Any],
    households: Sequence[Mapping[str, Any]],
    vehicles: Sequence[Mapping[str, Any]],
    shelters: Sequence[Mapping[str, Any]],
    routes: Sequence[Mapping[str, Any]],
    route_events: Sequence[Mapping[str, Any]],
    *,
    evacuation_start: str,
    external_vehicle_windows: Sequence[Mapping[str, Any]] = (),
    external_shelter_windows: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """生成三个确定性候选组合，逐个标注可行性与违反原因。"""
    intervals = (
        ("safety_first", 60, "nearest"),
        ("fast_clearance", 30, "nearest"),
        ("shelter_balanced", 60, "balanced"),
    )
    return [
        build_candidate(
            warning,
            households,
            vehicles,
            shelters,
            routes,
            route_events,
            evacuation_start=evacuation_start,
            interval_minutes=interval,
            strategy=strategy,
            label=label,
            external_vehicle_windows=external_vehicle_windows,
            external_shelter_windows=external_shelter_windows,
        )
        for label, interval, strategy in intervals
    ]


def build_candidate(
    warning: Mapping[str, Any],
    households: Sequence[Mapping[str, Any]],
    vehicles: Sequence[Mapping[str, Any]],
    shelters: Sequence[Mapping[str, Any]],
    routes: Sequence[Mapping[str, Any]],
    route_events: Sequence[Mapping[str, Any]],
    *,
    evacuation_start: str,
    interval_minutes: int,
    strategy: str,
    label: str,
    external_vehicle_windows: Sequence[Mapping[str, Any]] = (),
    external_shelter_windows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    deadline = warning["commit_deadline_at"]
    earliest_return = warning["earliest_return_at"]
    warning_version = warning["version"]
    t0 = _dt(evacuation_start)
    interval = timedelta(minutes=interval_minutes)
    stage_bases = [t0, t0 + interval]
    events_by_route: dict[str, list[Mapping[str, Any]]] = {}
    for event in route_events:
        events_by_route.setdefault(event["route_id"], []).append(event)
    routes_by_id = {route["route_id"]: route for route in routes}
    shelters_by_id = {shelter["shelter_id"]: shelter for shelter in shelters}
    routes_by_village: dict[str, list[Mapping[str, Any]]] = {}
    for route in routes:
        routes_by_village.setdefault(route["village_id"], []).append(route)
    for village_routes in routes_by_village.values():
        village_routes.sort(key=lambda item: (int(item["minutes"]), item["route_id"]))

    active_vehicles = [v for v in vehicles if v.get("active", 1)]
    active_vehicles = sorted(active_vehicles, key=lambda item: item["vehicle_id"])

    ordered = sorted(
        households,
        key=lambda h: (
            0 if (h["vulnerable"] or h["dangerous_house"]) else 1,
            h["village_id"],
            0 if h["vulnerable"] else 1,
            h["household_id"],
        ),
    )
    groups = (
        [h for h in ordered if h["vulnerable"] or h["dangerous_house"]],
        [h for h in ordered if not (h["vulnerable"] or h["dangerous_house"])],
    )

    violations: set[str] = set()
    stages: list[dict[str, Any]] = []
    # trips[(vehicle_id, depart_iso)] = 共享同一车次的批次数
    trips: dict[tuple[str, str], dict[str, Any]] = {}
    assigned_per_shelter: dict[str, int] = {}
    throughput_used: dict[tuple[int, str], int] = {}
    all_batches: list[dict[str, Any]] = []

    for sequence, group in enumerate(groups, start=1):
        stage_depart = stage_bases[sequence - 1]
        batches: list[dict[str, Any]] = []
        for household in group:
            batch, local_violations = _assign_batch(
                household=household,
                sequence=sequence,
                stage_depart=stage_depart,
                interval=interval,
                interval_minutes=interval_minutes,
                deadline=deadline,
                strategy=strategy,
                routes_by_village=routes_by_village,
                routes_by_id=routes_by_id,
                events_by_route=events_by_route,
                vehicles=active_vehicles,
                trips=trips,
                assigned_per_shelter=assigned_per_shelter,
                shelters_by_id=shelters_by_id,
                external_vehicle_windows=external_vehicle_windows,
                throughput_used=throughput_used,
                warning_version=warning_version,
            )
            # 只有家庭最终无法安置时，尝试过程中收集到的原因才构成全局违规。
            if not batch.get("assigned"):
                violations.update(local_violations)
            batches.append(batch)
            all_batches.append(batch)
        if not batches:
            # 修订重算时某个优先级分组可能没有剩余家庭，跳过空阶段。
            continue
        stages.append({
            "sequence": len(stages) + 1,
            "scheduled_depart_at": _iso(stage_depart),
            "warning_version": warning_version,
            "batches": batches,
        })

    _check_shelter_beds(
        all_batches,
        shelters_by_id,
        earliest_return,
        external_shelter_windows,
        violations,
    )

    vehicle_trips = _summarize_trips(trips)
    reservations = _shelter_reservations(all_batches, earliest_return)
    feasible = not violations and all(batch.get("assigned", True) for batch in all_batches)
    return_plan = _return_plan(all_batches, vehicle_trips, earliest_return, interval_minutes)
    people = sum(int(batch["headcount"]) for batch in all_batches if batch.get("assigned", True))
    return {
        "candidate_id": label,
        "label": label,
        "interval_minutes": interval_minutes,
        "strategy": strategy,
        "evacuation_start": _iso(t0),
        "feasible": feasible,
        "violations": sorted(violations),
        "stages": stages,
        "vehicle_trips": vehicle_trips,
        "shelter_reservations": reservations,
        "return_plan": return_plan,
        "totals": {
            "households": len([b for b in all_batches if b.get("assigned", True)]),
            "people": people,
            "vulnerable_people": sum(
                int(batch["headcount"])
                for batch in all_batches
                if batch.get("assigned", True) and batch["vulnerable"]
            ),
            "vehicles_used": len({trip["vehicle_id"] for trip in vehicle_trips}),
        },
    }


def _assign_batch(
    *,
    household: Mapping[str, Any],
    sequence: int,
    stage_depart,
    interval: timedelta,
    interval_minutes: int,
    deadline: str,
    strategy: str,
    routes_by_village,
    routes_by_id,
    events_by_route,
    vehicles,
    trips,
    assigned_per_shelter,
    shelters_by_id,
    external_vehicle_windows,
    throughput_used,
    warning_version,
) -> tuple[dict[str, Any], set[str]]:
    local_violations: set[str] = set()
    base = {
        "household_id": household["household_id"],
        "village_id": household["village_id"],
        "headcount": household["members"],
        "vulnerable": bool(household["vulnerable"]),
        "dangerous_house": bool(household["dangerous_house"]),
        "beds": household["members"],
        "medical_beds": household["special_medical"],
        "assembly_point": household["assembly_point"],
        "transport_required": bool(household["transport_required"]),
    }
    village_routes = routes_by_village.get(household["village_id"], [])
    if not village_routes:
        local_violations.add(f"no_route:{household['village_id']}")
        return {**base, "assigned": False, "reason": "no_route"}, local_violations

    choices = _order_route_choices(
        strategy,
        village_routes,
        shelters_by_id,
        assigned_per_shelter,
    )
    need_wheelchair = household["mobility_impaired"]
    window_end_stage = stage_depart + interval
    for route in choices:
        events = events_by_route.get(route["route_id"], [])
        factor = ROUTE_FACTOR[route_state_at(events, _iso(stage_depart))]
        if factor == 0.0:
            local_violations.add(f"road_closed:{route['route_id']}")
            continue
        capacity_key = (sequence, route["route_id"])
        stage_capacity = math.floor(float(route["throughput_per_hour"]) * factor * interval_minutes / 60.0)
        if throughput_used.get(capacity_key, 0) + household["members"] > stage_capacity:
            local_violations.add(f"throughput:{route['route_id']}")
            continue
        minutes = timedelta(minutes=int(route["minutes"]))
        if not household["transport_required"]:
            depart = stage_depart
            arrive = depart + minutes
            if _iso(arrive) > deadline:
                local_violations.add("deadline_exceeded")
                continue
            throughput_used[capacity_key] = throughput_used.get(capacity_key, 0) + household["members"]
            assigned_per_shelter[route["shelter_id"]] = assigned_per_shelter.get(route["shelter_id"], 0) + household["members"]
            return {
                **base,
                "assigned": True,
                "route_id": route["route_id"],
                "vehicle_id": None,
                "shelter_id": route["shelter_id"],
                "depart_at": _iso(depart),
                "arrive_at": _iso(arrive),
            }, local_violations
        earliest_arrival = stage_depart + timedelta(minutes=int(route["minutes"]))
        if _iso(earliest_arrival) > deadline:
            local_violations.add("deadline_exceeded")
            continue
        placement = _find_vehicle_slot(
            vehicles=vehicles,
            route=route,
            events=events,
            trips=trips,
            headcount=household["members"],
            need_wheelchair=need_wheelchair,
            stage_depart=stage_depart,
            window_end_stage=window_end_stage,
            deadline=deadline,
            external_vehicle_windows=external_vehicle_windows,
        )
        if placement is None:
            local_violations.add(f"vehicle_capacity:{route['route_id']}")
            continue
        vehicle_id, depart, arrive = placement
        throughput_used[capacity_key] = throughput_used.get(capacity_key, 0) + household["members"]
        assigned_per_shelter[route["shelter_id"]] = assigned_per_shelter.get(route["shelter_id"], 0) + household["members"]
        key = (vehicle_id, _iso(depart))
        trip = trips.setdefault(key, {
            "vehicle_id": vehicle_id,
            "route_id": route["route_id"],
            "shelter_id": route["shelter_id"],
            "depart_at": _iso(depart),
            "arrive_at": _iso(depart + timedelta(minutes=int(route["minutes"]))),
            "return_at": _iso(depart + 2 * timedelta(minutes=int(route["minutes"]))),
            "seats_used": 0,
            "wheelchair_used": 0,
            "household_ids": [],
        })
        trip["seats_used"] += household["members"]
        trip["wheelchair_used"] += need_wheelchair
        trip["household_ids"].append(household["household_id"])
        return {
            **base,
            "assigned": True,
            "route_id": route["route_id"],
            "vehicle_id": vehicle_id,
            "shelter_id": route["shelter_id"],
            "depart_at": _iso(depart),
            "arrive_at": _iso(arrive),
        }, local_violations
    local_violations.add("vehicle_capacity" if household["transport_required"] else "deadline_exceeded")
    return {
        **base,
        "assigned": False,
        "reason": "vehicle_capacity" if household["transport_required"] else "deadline_exceeded",
    }, local_violations


def _order_route_choices(strategy, village_routes, shelters_by_id, assigned_per_shelter):
    if strategy != "balanced":
        return list(village_routes)
    def ratio(route):
        shelter = shelters_by_id.get(route["shelter_id"])
        total = int(shelter["beds_total"]) if shelter else 1
        return (assigned_per_shelter.get(route["shelter_id"], 0) / total, int(route["minutes"]), route["route_id"])
    return sorted(village_routes, key=ratio)


def _find_vehicle_slot(
    *,
    vehicles,
    route,
    events,
    trips,
    headcount,
    need_wheelchair,
    stage_depart,
    window_end_stage,
    deadline,
    external_vehicle_windows,
):
    """在阶段窗口内按往返周期扫描发车时间槽，返回第一辆可用车。

    同一时间槽上同一路线且有余位的车次允许拼车；车辆在往返周期内
    不能执行其他车次，也不能与已冻结方案的占用窗口重叠。
    """
    minutes = int(route["minutes"])
    cycle = timedelta(minutes=2 * minutes)
    depart = stage_depart
    while depart < window_end_stage:
        arrive = depart + timedelta(minutes=minutes)
        if _iso(arrive) > deadline:
            return None
        back = depart + cycle
        road_open = route_state_at(events, _iso(depart)) != "closed" and route_state_at(events, _iso(arrive)) != "closed"
        if road_open:
            for vehicle in vehicles:
                if vehicle["seats"] < headcount or vehicle["wheelchair_seats"] < need_wheelchair:
                    continue
                trip = trips.get((vehicle["vehicle_id"], _iso(depart)))
                if trip is not None:
                    if trip["route_id"] != route["route_id"]:
                        continue
                    if trip["seats_used"] + headcount > vehicle["seats"]:
                        continue
                    if trip["wheelchair_used"] + need_wheelchair > vehicle["wheelchair_seats"]:
                        continue
                if any(
                    other["vehicle_id"] == vehicle["vehicle_id"]
                    and other_key != (vehicle["vehicle_id"], _iso(depart))
                    and _overlaps(depart, back, _dt(other["depart_at"]), _dt(other["return_at"]))
                    for other_key, other in trips.items()
                ):
                    continue
                if any(
                    window["vehicle_id"] == vehicle["vehicle_id"]
                    and _overlaps(depart, back, _dt(window["window_start"]), _dt(window["window_end"]))
                    for window in external_vehicle_windows
                ):
                    continue
                return vehicle["vehicle_id"], depart, arrive
        depart += cycle
    return None


def _check_shelter_beds(all_batches, shelters_by_id, earliest_return, external_windows, violations):
    intervals: list[tuple[str, int, int, str, str]] = []
    for batch in all_batches:
        if not batch.get("assigned"):
            continue
        intervals.append((
            batch["shelter_id"],
            batch["beds"],
            batch["medical_beds"],
            batch["arrive_at"],
            earliest_return,
        ))
    for window in external_windows:
        intervals.append((
            window["shelter_id"],
            int(window["beds"]),
            int(window["medical_beds"]),
            window["window_start"],
            window["window_end"],
        ))
    for shelter_id in {item[0] for item in intervals}:
        points: list[tuple[str, int, int]] = []
        for sid, beds, medical, start, end in intervals:
            if sid != shelter_id:
                continue
            points.append((start, beds, medical))
            points.append((end, -beds, -medical))
        points.sort(key=lambda item: (item[0], -item[1]))
        beds_peak = medical_peak = 0
        beds_cur = medical_cur = 0
        for _, delta_beds, delta_medical in points:
            beds_cur += delta_beds
            medical_cur += delta_medical
            beds_peak = max(beds_peak, beds_cur)
            medical_peak = max(medical_peak, medical_cur)
        shelter = shelters_by_id.get(shelter_id)
        if shelter is None:
            continue
        if beds_peak > int(shelter["beds_total"]):
            violations.add(f"beds:{shelter_id}")
        if medical_peak > int(shelter["medical_beds_total"]):
            violations.add(f"medical_beds:{shelter_id}")


def _summarize_trips(trips) -> list[dict[str, Any]]:
    result = []
    for key in sorted(trips, key=lambda k: (trips[k]["depart_at"], trips[k]["vehicle_id"])):
        trip = trips[key]
        result.append({
            "vehicle_id": trip["vehicle_id"],
            "route_id": trip["route_id"],
            "shelter_id": trip["shelter_id"],
            "depart_at": trip["depart_at"],
            "arrive_at": trip["arrive_at"],
            "return_at": trip["return_at"],
            "seats_used": trip["seats_used"],
            "wheelchair_used": trip["wheelchair_used"],
            "household_ids": sorted(trip["household_ids"]),
        })
    return result


def _shelter_reservations(all_batches, earliest_return) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for batch in all_batches:
        if not batch.get("assigned"):
            continue
        item = grouped.setdefault(batch["shelter_id"], {
            "shelter_id": batch["shelter_id"],
            "beds": 0,
            "medical_beds": 0,
            "window_start": batch["arrive_at"],
            "window_end": earliest_return,
        })
        item["beds"] += batch["beds"]
        item["medical_beds"] += batch["medical_beds"]
        if batch["arrive_at"] < item["window_start"]:
            item["window_start"] = batch["arrive_at"]
    return [grouped[key] for key in sorted(grouped)]


def build_return_plan(trips, self_transport_ids, earliest_return, interval_minutes) -> dict[str, Any]:
    """返迁投影：车辆按撤离到达倒序错峰返迁，最早不早于返迁窗口。

    trips: 每个车次含 vehicle_id/route_id/shelter_id/arrive_at/household_ids。
    self_transport_ids: 自行撤离（无需车辆）的家庭，排在返迁窗口起点。
    """
    ordered_trips = sorted(trips, key=lambda trip: (trip["arrive_at"], trip["vehicle_id"]), reverse=True)
    return_stages = []
    base = _dt(earliest_return)
    for index, trip in enumerate(ordered_trips):
        depart = base + timedelta(minutes=interval_minutes * index)
        return_stages.append({
            "sequence": index + 1,
            "vehicle_id": trip["vehicle_id"],
            "route_id": trip["route_id"],
            "shelter_id": trip["shelter_id"],
            "depart_at": _iso(depart),
            "household_ids": list(trip["household_ids"]),
        })
    self_transport = sorted(self_transport_ids)
    if self_transport:
        return_stages.append({
            "sequence": len(return_stages) + 1,
            "vehicle_id": None,
            "route_id": None,
            "shelter_id": None,
            "depart_at": earliest_return,
            "household_ids": self_transport,
        })
    return_stages.sort(key=lambda stage: (stage["depart_at"], stage["sequence"]))
    for index, stage in enumerate(return_stages, start=1):
        stage["sequence"] = index
    return {
        "earliest_return_at": earliest_return,
        "stages": return_stages,
    }


def merge_return_plan(frozen_batches, candidate, earliest_return, interval_minutes, skip_return=()):
    """修订时把已冻结阶段（豁免/已返迁家庭除外）与新阶段并入同一返迁计划。"""
    skip = set(skip_return)
    grouped: dict[tuple, dict[str, Any]] = {}
    self_transport = set()
    for batch in frozen_batches:
        household_id = batch["household_id"]
        if household_id in skip:
            continue
        if batch.get("vehicle_id") and batch.get("depart_at"):
            key = (batch["vehicle_id"], batch["depart_at"])
            trip = grouped.setdefault(key, {
                "vehicle_id": batch["vehicle_id"],
                "route_id": batch.get("route_id"),
                "shelter_id": batch.get("shelter_id"),
                "arrive_at": batch["arrive_at"],
                "household_ids": [],
            })
            trip["household_ids"].append(household_id)
        else:
            self_transport.add(household_id)
    trips = list(grouped.values())
    trips.extend({
        "vehicle_id": trip["vehicle_id"],
        "route_id": trip["route_id"],
        "shelter_id": trip["shelter_id"],
        "arrive_at": trip["arrive_at"],
        "household_ids": [hid for hid in trip["household_ids"] if hid not in skip],
    } for trip in candidate["vehicle_trips"])
    trips = [trip for trip in trips if trip["household_ids"]]
    for batch in candidate["stages"]:
        for item in batch["batches"]:
            if item.get("assigned") and not item.get("frozen") and not item.get("transport_required", True):
                self_transport.add(item["household_id"])
    return build_return_plan(trips, self_transport, earliest_return, interval_minutes)


def _return_plan(all_batches, vehicle_trips, earliest_return, interval_minutes) -> dict[str, Any]:
    """返迁投影：按原撤离到达顺序倒序错峰，最早不早于返迁窗口。"""
    assigned = [b for b in all_batches if b.get("assigned")]
    self_transport = {b["household_id"] for b in assigned if not b["transport_required"]}
    trips = [{
        "vehicle_id": trip["vehicle_id"],
        "route_id": trip["route_id"],
        "shelter_id": trip["shelter_id"],
        "arrive_at": trip["arrive_at"],
        "household_ids": list(trip["household_ids"]),
    } for trip in vehicle_trips]
    return build_return_plan(trips, self_transport, earliest_return, interval_minutes)
