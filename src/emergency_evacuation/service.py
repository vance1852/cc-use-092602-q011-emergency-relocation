"""预警登记、候选组合生成、确认冻结、现场回执归并与查询用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

from .clock import SystemClock, utc_text
from .contracts import (
    CandidateOptions,
    HouseholdProfile,
    RoadSegment,
    Shelter,
    Vehicle,
    VillageBaseline,
    WarningRevision,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .planning import (
    build_candidate,
    canonical_json,
    digest,
    safe_revise_plan,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"baseline.write", "warning.write", "plan.generate", "report.read"},
    "dispatcher": {"resource.write", "plan.generate", "report.read"},
    "commander": {"plan.confirm", "plan.freeze", "warning.write", "return.write", "report.read"},
    "field": {"receipt.write", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

# 已进入执行阶段（车辆已发车或现场已有终态）的家庭，预警修订时保留。
EXECUTED_STATES = {"departed", "arrived", "sheltered", "returned", "absent", "refused", "medical_hold"}
# 成功性终态（已返迁）；失败性终态（失联、拒迁）允许被更晚的正面现场事件推进。
SUCCESS_TERMINAL_STATES = {"returned"}
FAILURE_TERMINAL_STATES = {"absent", "refused"}
TERMINAL_STATES = SUCCESS_TERMINAL_STATES | FAILURE_TERMINAL_STATES
EVACUATED_STATES = {"departed", "arrived", "sheltered", "returned"}
# 已安全抵达目的地（含入住临时住所与返迁）视为转移完成。
COMPLETED_STATES = {"arrived", "sheltered", "returned"}
# 失败性终态之后可被接受的更晚正面事件（找到人、改主意后撤离）。
RECOVERY_EVENTS = {"departed", "arrived", "sheltered", "returned", "medical_hold"}
UNFINISHED_REASON = {
    "pending": "not_started",
    "notified": "notified_not_departed",
    "departed": "in_transit",
    "medical_hold": "medical_hold",
    "absent": "absent",
    "refused": "refused",
}


class EvacuationService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    # ------------------------------------------------------------------ 用户

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM evac_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO evac_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM evac_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO evac_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    # ------------------------------------------------------------- 人口基线

    def register_village(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "baseline.write")
        village = VillageBaseline.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO village_baselines(village_id,name,households,population,"
                    "at_risk_households,revised_by,revised_at) VALUES(?,?,?,?,?,?,?)",
                    (village.village_id, village.name, village.households, village.population,
                     village.at_risk_households, actor_id, self._now()),
                )
                self._audit("village", village.village_id, "village.registered", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("村级编号已经存在") from exc
        return {"village_id": village.village_id, "households": village.households,
                "population": village.population, "at_risk_households": village.at_risk_households}

    def upsert_household(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "baseline.write")
        household = HouseholdProfile.from_dict(raw)
        if self.connection.execute(
            "SELECT 1 FROM village_baselines WHERE village_id=?", (household.village_id,)
        ).fetchone() is None:
            raise NotFound("行政村不存在")
        with transaction(self.connection, immediate=True):
            existing = self.connection.execute(
                "SELECT revision FROM households WHERE household_id=?", (household.household_id,)
            ).fetchone()
            if existing is None:
                self.connection.execute(
                    "INSERT INTO households(household_id,village_id,members,vulnerable_json,"
                    "transferable,transfer_kind,needs_ambulance,shelter_required,"
                    "family_assembly_point,medical_destination_id,revised_by,revised_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (household.household_id, household.village_id, household.members,
                     canonical_json([dict(item) for item in household.vulnerable]),
                     int(household.transferable), household.transfer_kind,
                     int(household.needs_ambulance), int(household.shelter_required),
                     household.family_assembly_point, household.medical_destination_id,
                     actor_id, self._now()),
                )
                event = "household.registered"
            else:
                self.connection.execute(
                    "UPDATE households SET village_id=?,members=?,vulnerable_json=?,transferable=?,"
                    "transfer_kind=?,needs_ambulance=?,shelter_required=?,"
                    "family_assembly_point=?,medical_destination_id=?,revised_by=?,revised_at=?,"
                    "revision=revision+1 WHERE household_id=?",
                    (household.village_id, household.members,
                     canonical_json([dict(item) for item in household.vulnerable]),
                     int(household.transferable), household.transfer_kind,
                     int(household.needs_ambulance), int(household.shelter_required),
                     household.family_assembly_point, household.medical_destination_id,
                     actor_id, self._now(), household.household_id),
                )
                event = "household.revised"
            self._audit("household", household.household_id, event, actor_id, dict(raw))
        return {"household_id": household.household_id, "village_id": household.village_id,
                "members": household.members, "transfer_kind": household.transfer_kind}

    # ----------------------------------------------------------- 车辆/床位/路

    def register_vehicle(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "resource.write")
        vehicle = Vehicle.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO vehicles(vehicle_id,kind,seats,ambulance,seats_for_mobility,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (vehicle.vehicle_id, vehicle.kind, vehicle.seats, int(vehicle.ambulance),
                     vehicle.seats_for_mobility, actor_id, self._now()),
                )
                self._audit("vehicle", vehicle.vehicle_id, "vehicle.registered", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("车辆编号已经存在") from exc
        return {"vehicle_id": vehicle.vehicle_id, "seats": vehicle.seats,
                "ambulance": vehicle.ambulance}

    def register_shelter(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "resource.write")
        shelter = Shelter.from_dict(raw)
        if self.connection.execute(
            "SELECT 1 FROM village_baselines WHERE village_id=?", (shelter.village_id,)
        ).fetchone() is None:
            raise NotFound("行政村不存在")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO shelters(shelter_id,name,village_id,beds,accessible_beds,"
                    "medical_beds,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (shelter.shelter_id, shelter.name, shelter.village_id, shelter.beds,
                     shelter.accessible_beds, shelter.medical_beds, actor_id, self._now()),
                )
                self._audit("shelter", shelter.shelter_id, "shelter.registered", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("临时住所编号已经存在") from exc
        return {"shelter_id": shelter.shelter_id, "beds": shelter.beds}

    def update_road(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "resource.write")
        road = RoadSegment.from_dict(raw)
        with transaction(self.connection, immediate=True):
            existing = self.connection.execute(
                "SELECT revision FROM road_segments WHERE road_id=? AND village_id=?",
                (road.road_id, road.village_id),
            ).fetchone()
            if existing is None:
                self.connection.execute(
                    "INSERT INTO road_segments(road_id,village_id,state,detour_minutes,"
                    "updated_by,updated_at) VALUES(?,?,?,?,?,?)",
                    (road.road_id, road.village_id, road.state, road.detour_minutes,
                     actor_id, self._now()),
                )
            else:
                self.connection.execute(
                    "UPDATE road_segments SET state=?,detour_minutes=?,updated_by=?,updated_at=?,"
                    "revision=revision+1 WHERE road_id=? AND village_id=?",
                    (road.state, road.detour_minutes, actor_id, self._now(),
                     road.road_id, road.village_id),
                )
            self._audit("road", f"{road.road_id}@{road.village_id}", "road.updated", actor_id,
                        {"state": road.state, "detour_minutes": road.detour_minutes})
        return {"road_id": road.road_id, "village_id": road.village_id, "state": road.state}

    # ----------------------------------------------------------------- 预警

    def record_warning(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "warning.write")
        warning = WarningRevision.from_dict(raw)
        for village_id in warning.village_ids:
            if self.connection.execute(
                "SELECT 1 FROM village_baselines WHERE village_id=?", (village_id,)
            ).fetchone() is None:
                raise NotFound(f"行政村 {village_id} 不存在")
        latest = self.connection.execute(
            "SELECT revision,state FROM warnings WHERE warning_id=? ORDER BY revision DESC LIMIT 1",
            (warning.warning_id,),
        ).fetchone()
        expected = 1 if latest is None else int(latest["revision"]) + 1
        if warning.revision != expected:
            raise Conflict(f"预警修订号必须是 {expected}")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO warnings(warning_id,revision,level,issued_at,deadline_at,"
                "village_ids_json,risk_curve_json,notes,state,recorded_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?, 'active', ?,?)",
                (warning.warning_id, warning.revision, warning.level, warning.issued_at,
                 warning.deadline_at, canonical_json(list(warning.village_ids)),
                 canonical_json([dict(point) for point in warning.curve_points]),
                 warning.notes, actor_id, self._now()),
            )
            if latest is not None:
                self.connection.execute(
                    "UPDATE warnings SET state='superseded' WHERE warning_id=? AND revision=?",
                    (warning.warning_id, latest["revision"]),
                )
            self._audit("warning", f"{warning.warning_id}:{warning.revision}",
                        "warning.revision_recorded", actor_id,
                        {"level": warning.level, "revision": warning.revision,
                         "deadline_at": warning.deadline_at})
        return {"warning_id": warning.warning_id, "revision": warning.revision,
                "level": warning.level, "state": "active"}

    def _latest_warning_row(self, warning_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM warnings WHERE warning_id=? ORDER BY revision DESC LIMIT 1",
            (warning_id,),
        ).fetchone()
        if row is None:
            raise NotFound("预警不存在")
        return row

    def warning_curve(self, warning_id: str) -> dict[str, Any]:
        """预警版本链与风险变化曲线。"""
        rows = self.connection.execute(
            "SELECT * FROM warnings WHERE warning_id=? ORDER BY revision", (warning_id,)
        ).fetchall()
        if not rows:
            raise NotFound("预警不存在")
        revisions = []
        for row in rows:
            curve = json.loads(row["risk_curve_json"])
            scores = [float(point["risk_score"]) for point in curve]
            revisions.append({
                "revision": row["revision"],
                "level": row["level"],
                "state": row["state"],
                "issued_at": row["issued_at"],
                "deadline_at": row["deadline_at"],
                "risk_score_first": scores[0],
                "risk_score_peak": max(scores),
                "risk_score_latest": scores[-1],
                "trend": "rising" if scores[-1] > scores[0] else (
                    "falling" if scores[-1] < scores[0] else "flat"),
                "points": curve,
            })
        return {"warning_id": warning_id, "revisions": revisions}

    # ------------------------------------------------------------- 资源读取

    def _household_rows(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM households ORDER BY village_id,household_id"
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["vulnerable"] = json.loads(row["vulnerable_json"])
            result.append(item)
        return result

    def _vehicle_rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM vehicles WHERE active=1 ORDER BY vehicle_id").fetchall()]

    def _shelter_rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM shelters WHERE active=1 ORDER BY shelter_id").fetchall()]

    def _road_rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM road_segments ORDER BY road_id,village_id").fetchall()]

    def _frozen_usage(self, exclude_plan_id: str | None = None) -> dict[str, dict[str, int]]:
        """汇总所有计划已冻结的资源占用。"""
        sql = ("SELECT plan_id,resource_kind,resource_id,reserved_seats,reserved_mobility,"
               "reserved_beds,reserved_accessible,reserved_medical FROM frozen_resources")
        params: tuple[Any, ...] = ()
        if exclude_plan_id is not None:
            sql += " WHERE plan_id<>?"
            params = (exclude_plan_id,)
        usage: dict[str, dict[str, int]] = {}
        for row in self.connection.execute(sql, params).fetchall():
            key = f"{row['resource_kind']}:{row['resource_id']}"
            bucket = usage.setdefault(key, {"seats": 0, "mobility": 0, "beds": 0,
                                            "accessible": 0, "medical": 0})
            bucket["seats"] += int(row["reserved_seats"])
            bucket["mobility"] += int(row["reserved_mobility"])
            bucket["beds"] += int(row["reserved_beds"])
            bucket["accessible"] += int(row["reserved_accessible"])
            bucket["medical"] += int(row["reserved_medical"])
        return usage

    def _projected_frozen_usage(
        self, parent_plan_id: str, retained: list[dict[str, Any]]
    ) -> dict[str, dict[str, int]]:
        """候选规划视角的冻结占用：父计划只计算已执行家庭的实际占用。"""
        usage = self._frozen_usage(exclude_plan_id=parent_plan_id)

        def add(key: str, **deltas: int) -> None:
            bucket = usage.setdefault(key, {"seats": 0, "mobility": 0, "beds": 0,
                                            "accessible": 0, "medical": 0})
            for name, delta in deltas.items():
                bucket[name] += delta

        for item in retained:
            mobility = sum(1 for person in item.get("vulnerable", []) if person["kind"] == "mobility")
            medical = sum(1 for person in item.get("vulnerable", []) if person["kind"] == "medical")
            if item["vehicle_id"]:
                add(f"vehicle:{item['vehicle_id']}", seats=int(item["members"]), mobility=mobility)
            if item["shelter_id"]:
                add(f"shelter:{item['shelter_id']}", beds=int(item["members"]),
                    accessible=mobility, medical=medical)
        return usage

    @staticmethod
    def _effective_vehicles(vehicles: list[dict[str, Any]],
                            frozen: Mapping[str, Mapping[str, int]]) -> list[dict[str, Any]]:
        result = []
        for vehicle in vehicles:
            used = frozen.get(f"vehicle:{vehicle['vehicle_id']}")
            if used is None:
                result.append(dict(vehicle))
                continue
            row = dict(vehicle)
            row["seats"] = max(0, int(vehicle["seats"]) - used["seats"])
            row["seats_for_mobility"] = max(0, int(vehicle["seats_for_mobility"]) - used["mobility"])
            result.append(row)
        return result

    @staticmethod
    def _effective_shelters(shelters: list[dict[str, Any]],
                            frozen: Mapping[str, Mapping[str, int]]) -> list[dict[str, Any]]:
        result = []
        for shelter in shelters:
            used = frozen.get(f"shelter:{shelter['shelter_id']}")
            if used is None:
                result.append(dict(shelter))
                continue
            row = dict(shelter)
            row["beds"] = max(0, int(shelter["beds"]) - used["beds"])
            row["accessible_beds"] = max(0, int(shelter["accessible_beds"]) - used["accessible"])
            row["medical_beds"] = max(0, int(shelter["medical_beds"]) - used["medical"])
            result.append(row)
        return result

    # ------------------------------------------------------- 候选组合与修订

    def generate_plan(
        self,
        actor_id: str,
        warning_id: str,
        raw_options: Mapping[str, Any] | None = None,
        *,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "plan.generate")
        options = CandidateOptions.from_dict(dict(raw_options or {}))
        warning_row = self._latest_warning_row(warning_id)
        warning = self._warning_view(warning_row)
        households = self._household_rows()
        roads = self._road_rows()

        parent = self.connection.execute(
            "SELECT * FROM evacuation_plans WHERE warning_id=? AND state='frozen' "
            "ORDER BY created_at DESC,plan_id DESC LIMIT 1",
            (warning_id,),
        ).fetchone()
        if parent is not None and int(parent["warning_revision"]) == warning["revision"]:
            raise InvalidState("当前预警版本方案已冻结，需先登记新的预警修订版本")
        stale_pending = self.connection.execute(
            "SELECT plan_id FROM evacuation_plans WHERE warning_id=? AND warning_revision=? "
            "AND state IN ('proposed','confirmed') ORDER BY created_at DESC",
            (warning_id, warning["revision"]),
        ).fetchall()
        unresolved = self.connection.execute(
            "SELECT plan_id FROM evacuation_plans WHERE warning_id=? AND warning_revision<>? "
            "AND state IN ('proposed','confirmed') LIMIT 1",
            (warning_id, warning["revision"]),
        ).fetchone()
        if unresolved is not None:
            raise InvalidState("上一预警版本的方案尚未冻结，请先确认冻结或作废后再按修订生成")

        with transaction(self.connection, immediate=True):
            retained: list[dict[str, Any]] = []
            executed: set[str] = set()
            if parent is not None:
                # 修订只影响尚未执行的阶段：候选规划时把父计划占用投影为仅覆盖已执行家庭。
                executed = self._executed_household_ids(parent["plan_id"])
                parent_candidate = json.loads(parent["candidate_json"])
                retained = [item for item in parent_candidate["assignments"]
                            if item["household_id"] in executed]
                frozen = self._projected_frozen_usage(parent["plan_id"], retained)
                based_on = parent["plan_id"]
            else:
                parent_candidate = None
                frozen = self._frozen_usage()
                based_on = None
            vehicles = self._effective_vehicles(self._vehicle_rows(), frozen)
            shelters = self._effective_shelters(self._shelter_rows(), frozen)
            if parent is not None:
                candidate = safe_revise_plan(
                    current_plan=parent_candidate,
                    latest_warning=warning,
                    households=households,
                    vehicles=vehicles,
                    shelters=shelters,
                    roads=roads,
                    executed_household_ids=executed,
                    batch_minutes=options.batch_minutes,
                    return_window_hours=options.return_window_hours,
                )
            else:
                candidate = build_candidate(
                    warning=warning, households=households, vehicles=vehicles, shelters=shelters,
                    roads=roads, batch_minutes=options.batch_minutes,
                    return_window_hours=options.return_window_hours,
                )

            input_value = {
                "warning": warning,
                "households": households,
                "vehicles": self._vehicle_rows(),
                "shelters": self._shelter_rows(),
                "roads": roads,
                "options": {"batch_minutes": options.batch_minutes,
                            "return_window_hours": options.return_window_hours},
                "based_on": based_on,
            }
            input_sha256 = digest(input_value)
            existing = self.connection.execute(
                "SELECT plan_id,candidate_json,state FROM evacuation_plans "
                "WHERE warning_id=? AND warning_revision=? AND input_sha256=? "
                "ORDER BY rowid DESC LIMIT 1",
                (warning_id, warning["revision"], input_sha256),
            ).fetchone()
            if existing is not None and existing["state"] in {"proposed", "confirmed"}:
                return {"plan_id": existing["plan_id"], "replayed": True,
                        "based_on_plan_id": based_on,
                        "candidate": json.loads(existing["candidate_json"])}
            if existing is not None and existing["state"] != "cancelled":
                raise Conflict("相同输入的方案已存在且处于不可复用状态")

            for stale in stale_pending:
                if existing is not None and stale["plan_id"] == existing["plan_id"]:
                    continue
                self.connection.execute(
                    "UPDATE evacuation_plans SET state='cancelled',revision=revision+1 "
                    "WHERE plan_id=? AND state IN ('proposed','confirmed')",
                    (stale["plan_id"],),
                )
                self._audit("plan", stale["plan_id"], "plan.cancelled", actor_id,
                            {"reason": "regenerated"})
            if existing is not None:
                # 相同输入的候选曾被作废：复活该行，保持输入哈希唯一。
                new_plan_id = existing["plan_id"]
                self.connection.execute(
                    "UPDATE evacuation_plans SET candidate_json=?,based_on_plan_id=?,"
                    "state='proposed',confirmed_by=NULL,confirmed_at=NULL,"
                    "frozen_by=NULL,frozen_at=NULL,created_by=?,created_at=?,"
                    "revision=revision+1 WHERE plan_id=?",
                    (canonical_json(candidate), based_on, actor_id, self._now(), new_plan_id),
                )
            else:
                new_plan_id = plan_id or (
                    f"plan-{warning_id}-r{warning['revision']}-" + digest(candidate)[:12])
                self.connection.execute(
                    "INSERT INTO evacuation_plans(plan_id,warning_id,warning_revision,"
                    "candidate_json,input_sha256,based_on_plan_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (new_plan_id, warning_id, warning["revision"], canonical_json(candidate),
                     input_sha256, based_on, actor_id, self._now()),
                )
            self._audit("plan", new_plan_id, "plan.proposed", actor_id,
                        {"warning_id": warning_id, "revision": warning["revision"],
                         "based_on": based_on,
                         "feasible": candidate["constraints"]["feasible"],
                         "uncovered": candidate["constraints"]["uncovered_count"]})
        return {"plan_id": new_plan_id, "replayed": False,
                "based_on_plan_id": based_on, "candidate": candidate}

    @staticmethod
    def _warning_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "warning_id": row["warning_id"],
            "revision": row["revision"],
            "level": row["level"],
            "issued_at": row["issued_at"],
            "deadline_at": row["deadline_at"],
            "village_ids": json.loads(row["village_ids_json"]),
            "risk_curve": json.loads(row["risk_curve_json"]),
        }

    def _executed_household_ids(self, plan_id: str) -> set[str]:
        rows = self.connection.execute(
            "SELECT household_id,state FROM household_progress WHERE plan_id=?", (plan_id,)
        ).fetchall()
        return {row["household_id"] for row in rows if row["state"] in EXECUTED_STATES}

    def _sync_frozen_rows(self, plan_id: str, retained: list[dict[str, Any]]) -> None:
        vehicle_use: dict[str, dict[str, int]] = {}
        shelter_use: dict[str, dict[str, int]] = {}
        for item in retained:
            mobility = sum(1 for person in item.get("vulnerable", []) if person["kind"] == "mobility")
            medical = sum(1 for person in item.get("vulnerable", []) if person["kind"] == "medical")
            if item["vehicle_id"]:
                bucket = vehicle_use.setdefault(item["vehicle_id"], {"seats": 0, "mobility": 0})
                bucket["seats"] += int(item["members"])
                bucket["mobility"] += mobility
            if item["shelter_id"]:
                bucket = shelter_use.setdefault(item["shelter_id"],
                                                {"beds": 0, "accessible": 0, "medical": 0})
                bucket["beds"] += int(item["members"])
                bucket["accessible"] += mobility
                bucket["medical"] += medical
        self.connection.execute("DELETE FROM frozen_resources WHERE plan_id=?", (plan_id,))
        frozen_at = self._now()
        for resource_id, use in vehicle_use.items():
            self.connection.execute(
                "INSERT INTO frozen_resources(plan_id,resource_kind,resource_id,reserved_seats,"
                "reserved_mobility,frozen_at) VALUES(?, 'vehicle', ?,?,?,?)",
                (plan_id, resource_id, use["seats"], use["mobility"], frozen_at),
            )
        for resource_id, use in shelter_use.items():
            self.connection.execute(
                "INSERT INTO frozen_resources(plan_id,resource_kind,resource_id,reserved_beds,"
                "reserved_accessible,reserved_medical,frozen_at) VALUES(?, 'shelter', ?,?,?,?,?)",
                (plan_id, resource_id, use["beds"], use["accessible"], use["medical"], frozen_at),
            )

    # ------------------------------------------------------------- 确认/冻结

    def _plan_row(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM evacuation_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("转移方案不存在")
        return row

    def confirm_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.confirm")
        with transaction(self.connection, immediate=True):
            row = self._plan_row(plan_id)
            if row["revision"] != expected_revision:
                raise Conflict("方案版本不匹配")
            if row["state"] != "proposed":
                raise InvalidState("只有候选方案可以确认")
            candidate = json.loads(row["candidate_json"])
            self.connection.execute(
                "UPDATE evacuation_plans SET state='confirmed',confirmed_by=?,confirmed_at=?,"
                "revision=revision+1 WHERE plan_id=?",
                (actor_id, self._now(), plan_id),
            )
            self._audit("plan", plan_id, "plan.confirmed", actor_id,
                        {"feasible": candidate["constraints"]["feasible"]})
        return {"plan_id": plan_id, "state": "confirmed", "revision": expected_revision + 1}

    def freeze_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        """负责人确认后一次性冻结方案涉及的车辆与床位。

        修订方案冻结时接管父方案：父方案的冻结占用先收敛到已执行家庭，
        再标记为已修订（superseded），避免已执行终态被重置。
        """
        self._require(actor_id, "plan.freeze")
        with transaction(self.connection, immediate=True):
            row = self._plan_row(plan_id)
            if row["revision"] != expected_revision:
                raise Conflict("方案版本不匹配")
            if row["state"] != "confirmed":
                raise InvalidState("只有已确认方案可以冻结")
            candidate = json.loads(row["candidate_json"])
            if not candidate["constraints"]["feasible"]:
                raise InvalidState(
                    "方案未满足重点人群、道路通行或返迁窗口约束，不能冻结："
                    + ",".join(candidate["constraints"]["violations"])
                    + ",".join(candidate["constraints"]["uncovered_reasons"]))

            parent_id = row["based_on_plan_id"]
            if parent_id:
                parent = self._plan_row(parent_id)
                if parent["state"] != "superseded":
                    executed = self._executed_household_ids(parent_id)
                    parent_candidate = json.loads(parent["candidate_json"])
                    retained = [item for item in parent_candidate["assignments"]
                                if item["household_id"] in executed]
                    self._sync_frozen_rows(parent_id, retained)
                    self.connection.execute(
                        "UPDATE evacuation_plans SET state='superseded',revision=revision+1 "
                        "WHERE plan_id=? AND state='frozen'",
                        (parent_id,),
                    )
                    self._audit("plan", parent_id, "plan.superseded", actor_id,
                                {"by_plan": plan_id, "retained_households": len(retained)})

            self._assert_freeze_capacity(candidate)
            frozen_at = self._now()
            for vehicle in candidate["vehicles"]:
                self.connection.execute(
                    "INSERT INTO frozen_resources(plan_id,resource_kind,resource_id,reserved_seats,"
                    "reserved_mobility,frozen_at) VALUES(?, 'vehicle', ?,?,?,?)",
                    (plan_id, vehicle["vehicle_id"], vehicle["used_seats"],
                     vehicle["used_mobility"], frozen_at),
                )
            for shelter in candidate["shelters"]:
                self.connection.execute(
                    "INSERT INTO frozen_resources(plan_id,resource_kind,resource_id,reserved_beds,"
                    "reserved_accessible,reserved_medical,frozen_at) VALUES(?, 'shelter', ?,?,?,?,?)",
                    (plan_id, shelter["shelter_id"], shelter["reserved_beds"],
                     shelter["reserved_accessible"], shelter["reserved_medical"], frozen_at),
                )
            self.connection.execute(
                "UPDATE evacuation_plans SET state='frozen',frozen_by=?,frozen_at=?,"
                "revision=revision+1 WHERE plan_id=?",
                (actor_id, frozen_at, plan_id),
            )
            self._audit("plan", plan_id, "plan.frozen", actor_id,
                        {"vehicles": len(candidate["vehicles"]),
                         "shelters": len(candidate["shelters"])})
        return {"plan_id": plan_id, "state": "frozen", "revision": expected_revision + 1,
                "frozen_vehicles": len(candidate["vehicles"]),
                "frozen_shelters": len(candidate["shelters"])}

    def _assert_freeze_capacity(self, candidate: Mapping[str, Any]) -> None:
        frozen = self._frozen_usage()
        vehicles = {row["vehicle_id"]: row for row in self._vehicle_rows()}
        for plan_vehicle in candidate["vehicles"]:
            nominal = vehicles.get(plan_vehicle["vehicle_id"])
            if nominal is None:
                raise InvalidState(f"车辆 {plan_vehicle['vehicle_id']} 已停用")
            used = frozen.get(f"vehicle:{plan_vehicle['vehicle_id']}",
                              {"seats": 0, "mobility": 0})
            if used["seats"] + plan_vehicle["used_seats"] > nominal["seats"]:
                raise Conflict(f"车辆 {plan_vehicle['vehicle_id']} 座位冻结冲突")
            if used["mobility"] + plan_vehicle["used_mobility"] > nominal["seats_for_mobility"]:
                raise Conflict(f"车辆 {plan_vehicle['vehicle_id']} 无障碍座位冻结冲突")
        shelters = {row["shelter_id"]: row for row in self._shelter_rows()}
        for plan_shelter in candidate["shelters"]:
            nominal = shelters.get(plan_shelter["shelter_id"])
            if nominal is None:
                raise InvalidState(f"临时住所 {plan_shelter['shelter_id']} 已停用")
            used = frozen.get(f"shelter:{plan_shelter['shelter_id']}",
                              {"beds": 0, "accessible": 0, "medical": 0})
            if used["beds"] + plan_shelter["reserved_beds"] > nominal["beds"]:
                raise Conflict(f"临时住所 {plan_shelter['shelter_id']} 床位冻结冲突")
            if used["accessible"] + plan_shelter["reserved_accessible"] > nominal["accessible_beds"]:
                raise Conflict(f"临时住所 {plan_shelter['shelter_id']} 无障碍床位冻结冲突")
            if used["medical"] + plan_shelter["reserved_medical"] > nominal["medical_beds"]:
                raise Conflict(f"临时住所 {plan_shelter['shelter_id']} 医疗床位冻结冲突")

    # ------------------------------------------------------------- 现场回执

    def record_receipt(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记现场回执；按事件时间归并，乱序/重复回执不破坏已确认终态。"""
        self._require(actor_id, "receipt.write")
        plan_id = str(raw.get("plan_id", "")).strip()
        receipt_id = str(raw.get("receipt_id", "")).strip()
        event_type = str(raw.get("event_type", "")).strip()
        if not plan_id or not receipt_id:
            raise ValidationFailed("plan_id 和 receipt_id 不能为空")
        if event_type not in {"notified", "departed", "arrived", "sheltered", "returned",
                              "absent", "refused", "medical_hold"}:
            raise ValidationFailed("event_type 不受支持")
        event_at = str(raw.get("event_at", "")).strip()
        from .clock import parse_utc
        try:
            event_dt = parse_utc(event_at, "event_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        event_at = utc_text(event_dt)
        household_id = str(raw.get("household_id", "")).strip()
        if not household_id:
            raise ValidationFailed("household_id 不能为空")
        persons = raw.get("persons")
        if isinstance(persons, bool) or not isinstance(persons, int) or persons < 0:
            raise ValidationFailed("persons 必须是非负整数")
        household = self.connection.execute(
            "SELECT members FROM households WHERE household_id=?", (household_id,)
        ).fetchone()
        if household is None:
            raise NotFound("家庭不存在")
        if persons > household["members"]:
            raise ValidationFailed("回执人数不能超过家庭人数")
        batch_no = raw.get("batch_no")
        if batch_no is not None and (isinstance(batch_no, bool) or not isinstance(batch_no, int)):
            raise ValidationFailed("batch_no 必须是整数")
        note = str(raw.get("note", ""))
        if len(note) > 256:
            raise ValidationFailed("note 不能超过 256 个字符")

        plan = self._plan_row(plan_id)
        if plan["state"] not in {"frozen", "superseded"}:
            raise InvalidState("方案尚未冻结，不能登记现场回执")

        with transaction(self.connection, immediate=True):
            duplicate = self.connection.execute(
                "SELECT receipt_id,event_type,event_at FROM field_receipts "
                "WHERE plan_id=? AND receipt_id=?",
                (plan_id, receipt_id),
            ).fetchone()
            if duplicate is not None:
                if duplicate["event_type"] != event_type or duplicate["event_at"] != event_at:
                    raise Conflict("回执编号对应不同事件")
                self._audit("receipt", f"{plan_id}:{receipt_id}", "receipt.duplicate_ignored",
                            actor_id, {"household_id": household_id})
                merged = self._merged_progress(plan_id).get(household_id)
                return {"plan_id": plan_id, "receipt_id": receipt_id, "applied": False,
                        "reason": "duplicate",
                        "state": None if merged is None else merged["state"]}

            current = self._merged_progress(plan_id).get(household_id)
            event_seq = int(self.connection.execute(
                "SELECT COALESCE(MAX(event_seq),0)+1 AS next_seq FROM field_receipts WHERE plan_id=?",
                (plan_id,),
            ).fetchone()["next_seq"])

            reason = "applied"
            if current is not None:
                same_plan = current.get("progress_plan_id") == plan_id
                older = event_at < current["event_at"]
                same_time_older_seq = same_plan and event_at == current["event_at"] and event_seq <= current["updated_seq"]
                if older or same_time_older_seq:
                    reason = "out_of_order"
                elif current["terminal"]:
                    if current["state"] in FAILURE_TERMINAL_STATES:
                        # 失联/拒迁是失败性终态：更晚的正面现场事件（找到人、改主意撤离）
                        # 可以推进状态；同刻或更早事件仍受保护。
                        if event_at <= current["event_at"] or event_type not in (
                                RECOVERY_EVENTS | FAILURE_TERMINAL_STATES):
                            reason = "terminal_protected"
                    elif event_type != "returned" or event_at <= current["event_at"]:
                        # 已返迁是成功性终态，只接受更晚的返迁更正。
                        reason = "terminal_protected"

            self.connection.execute(
                "INSERT INTO field_receipts(receipt_id,event_seq,plan_id,household_id,batch_no,"
                "vehicle_id,shelter_id,event_type,event_at,persons,note,recorded_by,received_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (receipt_id, event_seq, plan_id, household_id, batch_no,
                 raw.get("vehicle_id"), raw.get("shelter_id"), event_type, event_at, persons,
                 note, actor_id, self._now()),
            )

            if reason == "applied":
                terminal = int(event_type in TERMINAL_STATES)
                local = self.connection.execute(
                    "SELECT 1 FROM household_progress WHERE plan_id=? AND household_id=?",
                    (plan_id, household_id),
                ).fetchone()
                if local is None:
                    # 修订后的计划对该户可能尚无进度行（上一版进度已通过合并视图读取）。
                    self.connection.execute(
                        "INSERT INTO household_progress(plan_id,household_id,state,persons,"
                        "event_at,batch_no,vehicle_id,shelter_id,note,updated_seq,terminal) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (plan_id, household_id, event_type, persons, event_at, batch_no,
                         raw.get("vehicle_id"), raw.get("shelter_id"), note, event_seq, terminal),
                    )
                else:
                    self.connection.execute(
                        "UPDATE household_progress SET state=?,persons=?,event_at=?,batch_no=?,"
                        "vehicle_id=?,shelter_id=?,note=?,updated_seq=?,terminal=? "
                        "WHERE plan_id=? AND household_id=?",
                        (event_type, persons, event_at, batch_no, raw.get("vehicle_id"),
                         raw.get("shelter_id"), note, event_seq, terminal, plan_id, household_id),
                    )
            self._audit("receipt", f"{plan_id}:{receipt_id}", "receipt.recorded", actor_id,
                        {"household_id": household_id, "event_type": event_type,
                         "event_at": event_at, "merge": reason})
            state = event_type if reason == "applied" else current["state"]
        return {"plan_id": plan_id, "receipt_id": receipt_id, "applied": reason == "applied",
                "reason": reason, "state": state}

    # ---------------------------------------------------------------- 返迁

    def register_return_plan(self, actor_id: str, plan_id: str,
                             raw: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._require(actor_id, "return.write")
        raw = raw or {}
        plan = self._plan_row(plan_id)
        if plan["state"] not in {"frozen", "superseded"}:
            raise InvalidState("方案冻结后才能登记返迁计划")
        candidate = json.loads(plan["candidate_json"])
        window = candidate["return_window"]
        earliest = str(raw.get("earliest_return_at") or window["earliest_return_at"])
        return_by = str(raw.get("return_by_at") or window["return_by_at"])
        from .clock import parse_utc
        earliest_dt = parse_utc(earliest, "earliest_return_at")
        return_by_dt = parse_utc(return_by, "return_by_at")
        if return_by_dt <= earliest_dt:
            raise ValidationFailed("return_by_at 必须晚于 earliest_return_at")
        warning = self.connection.execute(
            "SELECT deadline_at FROM warnings WHERE warning_id=? AND revision=?",
            (plan["warning_id"], plan["warning_revision"]),
        ).fetchone()
        if earliest_dt < parse_utc(warning["deadline_at"]):
            raise ValidationFailed("最早返迁时间不能早于转移承诺窗口结束")
        planned_villages = sorted({
            assignment["village_id"] for assignment in candidate["assignments"]})
        note = str(raw.get("note", ""))[:256]
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO return_plans(plan_id,earliest_return_at,return_by_at,"
                "village_ids_json,note,created_by,created_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(plan_id) DO UPDATE SET earliest_return_at=excluded.earliest_return_at,"
                "return_by_at=excluded.return_by_at,village_ids_json=excluded.village_ids_json,"
                "note=excluded.note,created_by=excluded.created_by,created_at=excluded.created_at",
                (plan_id, utc_text(earliest_dt), utc_text(return_by_dt),
                 canonical_json(planned_villages), note, actor_id, self._now()),
            )
            self._audit("plan", plan_id, "return.registered", actor_id,
                        {"earliest_return_at": utc_text(earliest_dt),
                         "return_by_at": utc_text(return_by_dt)})
        return {"plan_id": plan_id, "earliest_return_at": utc_text(earliest_dt),
                "return_by_at": utc_text(return_by_dt), "village_ids": planned_villages}

    # ------------------------------------------------------------------ 查询

    def _resource_usage_view(
        self, plan_id: str, candidate: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """从方案派工结果汇总整车/整所占用（修订方案含已执行保留部分）。"""
        vehicle_use: dict[str, dict[str, Any]] = {}
        shelter_use: dict[str, dict[str, Any]] = {}
        for assignment in candidate["assignments"]:
            mobility = sum(1 for person in assignment.get("vulnerable", [])
                          if person["kind"] == "mobility")
            medical = sum(1 for person in assignment.get("vulnerable", [])
                         if person["kind"] == "medical")
            vehicle_id = assignment.get("vehicle_id")
            if vehicle_id:
                bucket = vehicle_use.setdefault(vehicle_id, {
                    "used_seats": 0, "used_mobility": 0, "villages": set(),
                    "household_ids": []})
                bucket["used_seats"] += int(assignment["members"])
                bucket["used_mobility"] += mobility
                bucket["villages"].add(assignment["village_id"])
                bucket["household_ids"].append(assignment["household_id"])
            shelter_id = assignment.get("shelter_id")
            if shelter_id:
                bucket = shelter_use.setdefault(shelter_id, {
                    "reserved_beds": 0, "reserved_accessible": 0, "reserved_medical": 0,
                    "village_id": assignment["village_id"]})
                bucket["reserved_beds"] += int(assignment["members"])
                bucket["reserved_accessible"] += mobility
                bucket["reserved_medical"] += medical
        vehicles_registry = {row["vehicle_id"]: row for row in self._vehicle_rows()}
        vehicles = []
        for vehicle_id in sorted(vehicle_use):
            use = vehicle_use[vehicle_id]
            nominal = vehicles_registry.get(vehicle_id)
            vehicles.append({
                "vehicle_id": vehicle_id,
                "ambulance": bool(nominal["ambulance"]) if nominal else False,
                "seats": int(nominal["seats"]) if nominal else use["used_seats"],
                "used_seats": use["used_seats"],
                "seats_for_mobility": int(nominal["seats_for_mobility"]) if nominal else use["used_mobility"],
                "used_mobility": use["used_mobility"],
                "village_ids": sorted(use["villages"]),
                "household_ids": sorted(use["household_ids"]),
            })
        shelters_registry = {row["shelter_id"]: row for row in self._shelter_rows()}
        shelters = []
        for shelter_id in sorted(shelter_use):
            use = shelter_use[shelter_id]
            nominal = shelters_registry.get(shelter_id)
            shelters.append({
                "shelter_id": shelter_id,
                "village_id": use["village_id"],
                "beds": int(nominal["beds"]) if nominal else use["reserved_beds"],
                "reserved_beds": use["reserved_beds"],
                "accessible_beds": int(nominal["accessible_beds"]) if nominal else use["reserved_accessible"],
                "reserved_accessible": use["reserved_accessible"],
                "medical_beds": int(nominal["medical_beds"]) if nominal else use["reserved_medical"],
                "reserved_medical": use["reserved_medical"],
            })
        return vehicles, shelters

    def _merged_progress(self, plan_id: str) -> dict[str, dict[str, Any]]:
        """沿 based_on 修订链收集每户最新进度（按事件时间、序号归并）。"""
        chain: list[sqlite3.Row] = []
        current_id: str | None = plan_id
        while current_id:
            row = self._plan_row(current_id)
            chain.append(row)
            current_id = row["based_on_plan_id"]
        merged: dict[str, dict[str, Any]] = {}
        for plan in reversed(chain):
            rows = self.connection.execute(
                "SELECT * FROM household_progress WHERE plan_id=? ORDER BY event_at,updated_seq",
                (plan["plan_id"],),
            ).fetchall()
            for row in rows:
                item = dict(row)
                item["progress_plan_id"] = plan["plan_id"]
                existing = merged.get(row["household_id"])
                if existing is None or (item["event_at"], item["updated_seq"]) >= (
                        existing["event_at"], existing["updated_seq"]):
                    merged[row["household_id"]] = item
        return merged

    def plan_status(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        plan = self._plan_row(plan_id)
        candidate = json.loads(plan["candidate_json"])
        progress = self._merged_progress(plan_id)
        household_rows = {row["household_id"]: row for row in self._household_rows()}

        planned_ids = [item["household_id"] for item in candidate["assignments"]]
        actual_evacuated = 0
        sheltered = 0
        returned = 0
        unfinished: list[dict[str, Any]] = []
        uncoverable_ids = {item["household_id"] for item in candidate["uncovered"]}
        for household_id in planned_ids:
            current = progress.get(household_id)
            state = "pending" if current is None else current["state"]
            persons = int(household_rows[household_id]["members"]) if current is None else int(current["persons"])
            if state in EVACUATED_STATES:
                actual_evacuated += persons
            if state == "sheltered":
                sheltered += persons
            if state == "returned":
                returned += persons
            if state not in COMPLETED_STATES:
                reason = "uncoverable" if state == "pending" and household_id in uncoverable_ids \
                    else UNFINISHED_REASON.get(state, state)
                unfinished.append({"household_id": household_id,
                                   "village_id": candidate_village(candidate, household_id),
                                   "state": state, "persons": persons, "reason": reason})
        # 规划阶段就无法覆盖的家庭同样计入未完成。
        for item in candidate["uncovered"]:
            if not any(entry["household_id"] == item["household_id"] for entry in unfinished):
                household = household_rows.get(item["household_id"])
                unfinished.append({"household_id": item["household_id"],
                                   "village_id": item["village_id"],
                                   "state": "uncoverable",
                                   "persons": int(household["members"]) if household else 0,
                                   "reason": item["reason"]})

        villages = self._village_impact(plan, candidate, progress, household_rows)
        return_plan = self._return_view(plan_id, candidate)
        vehicles, shelters = self._resource_usage_view(plan_id, candidate)
        batches = []
        for batch in candidate["batches"]:
            states = [progress.get(hid, {}).get("state", "pending") for hid in batch["household_ids"]]
            executed_count = sum(1 for state in states if state in EXECUTED_STATES)
            batches.append({**batch, "executed_households": executed_count,
                            "state": "executed" if executed_count == len(states) else (
                                "in_progress" if executed_count else "waiting")})
        return {
            "plan_id": plan_id,
            "warning_id": plan["warning_id"],
            "warning_revision": plan["warning_revision"],
            "state": plan["state"],
            "based_on_plan_id": plan["based_on_plan_id"],
            "constraints": candidate["constraints"],
            "planned_households": candidate["planned_households"],
            "planned_persons": candidate["planned_persons"],
            "actual_evacuated_persons": actual_evacuated,
            "sheltered_persons": sheltered,
            "returned_persons": returned,
            "protected": candidate.get("protected"),
            "retained_household_ids": candidate.get("retained_household_ids", []),
            "revised_household_ids": candidate.get("revised_household_ids", []),
            "batches": batches,
            "vehicles": vehicles,
            "shelters": shelters,
            "uncovered": candidate["uncovered"],
            "unfinished": sorted(unfinished, key=lambda item: item["household_id"]),
            "villages": villages,
            "return_plan": return_plan,
        }

    def _village_impact(
        self,
        plan: sqlite3.Row,
        candidate: Mapping[str, Any],
        progress: Mapping[str, dict[str, Any]],
        household_rows: Mapping[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        baseline_rows = {
            row["village_id"]: dict(row)
            for row in self.connection.execute("SELECT * FROM village_baselines").fetchall()
        }
        impact: dict[str, dict[str, Any]] = {}
        for village_id in self._warning_view(
                self.connection.execute(
                    "SELECT * FROM warnings WHERE warning_id=? AND revision=?",
                    (plan["warning_id"], plan["warning_revision"]),
                ).fetchone())["village_ids"]:
            base = baseline_rows.get(village_id, {"households": 0, "population": 0,
                                                  "at_risk_households": 0, "name": village_id})
            impact[village_id] = {
                "village_id": village_id,
                "name": base.get("name", village_id),
                "baseline_households": base["households"],
                "baseline_population": base["population"],
                "at_risk_households": base["at_risk_households"],
                "planned_households": 0,
                "planned_persons": 0,
                "actual_evacuated_persons": 0,
                "returned_persons": 0,
                "uncovered": [],
                "unfinished_reasons": {},
            }
        for assignment in candidate["assignments"]:
            village_id = assignment["village_id"]
            bucket = impact.get(village_id)
            if bucket is None:
                continue
            bucket["planned_households"] += 1
            bucket["planned_persons"] += int(assignment["members"])
            current = progress.get(assignment["household_id"])
            if current is not None and current["state"] in EVACUATED_STATES:
                bucket["actual_evacuated_persons"] += int(current["persons"])
            if current is not None and current["state"] == "returned":
                bucket["returned_persons"] += int(current["persons"])
            if current is None or current["state"] not in COMPLETED_STATES:
                state = "pending" if current is None else current["state"]
                reason = "uncoverable" if state == "pending" and assignment["household_id"] in {
                    item["household_id"] for item in candidate["uncovered"]
                } else UNFINISHED_REASON.get(state, state)
                bucket["unfinished_reasons"][reason] = bucket["unfinished_reasons"].get(reason, 0) + 1
        for item in candidate["uncovered"]:
            bucket = impact.get(item["village_id"])
            if bucket is not None:
                bucket["uncovered"].append({"household_id": item["household_id"],
                                            "reason": item["reason"]})
                bucket["unfinished_reasons"][item["reason"]] = bucket["unfinished_reasons"].get(
                    item["reason"], 0) + 1
        return [impact[key] for key in sorted(impact)]

    def _return_view(self, plan_id: str, candidate: Mapping[str, Any]) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM return_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is not None:
            return {"registered": True, "earliest_return_at": row["earliest_return_at"],
                    "return_by_at": row["return_by_at"],
                    "village_ids": json.loads(row["village_ids_json"]), "note": row["note"]}
        window = candidate["return_window"]
        return {"registered": False, "earliest_return_at": window["earliest_return_at"],
                "return_by_at": window["return_by_at"],
                "village_ids": sorted({a["village_id"] for a in candidate["assignments"]})}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM evac_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}


def candidate_village(candidate: Mapping[str, Any], household_id: str) -> str:
    for assignment in candidate["assignments"]:
        if assignment["household_id"] == household_id:
            return str(assignment["village_id"])
    for item in candidate["uncovered"]:
        if item["household_id"] == household_id:
            return str(item["village_id"])
    return ""
