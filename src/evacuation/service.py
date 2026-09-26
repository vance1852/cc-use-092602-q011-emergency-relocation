"""预警登记、人口基线、候选组合、确认冻结与现场回执的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    RECEIPT_EVENTS,
    HouseholdRecord,
    RouteRecord,
    ShelterRecord,
    VehicleRecord,
    WarningNotice,
)
from .planning import (
    canonical_json,
    digest,
    generate_candidates,
    merge_return_plan,
    route_state_at,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "risk": {"warning.write", "route_status.write", "report.read"},
    "planner": {"baseline.write", "plan.generate", "plan.revise", "report.read"},
    "director": {"plan.confirm", "report.read"},
    "dispatcher": {"receipt.write", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

EVENT_RANK = {"notified": 1, "departed": 2, "exempt": 3, "arrived": 4, "returned": 5}
TERMINAL_EVENTS = frozenset({"arrived", "exempt", "returned"})
BATCH_STATE = {
    "notified": "notified",
    "departed": "in_transit",
    "arrived": "delivered",
    "exempt": "exempt",
    "returned": "returned",
}


class EvacuationService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    # ------------------------------------------------------------------ 用户与审计

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

    # ------------------------------------------------------------------ 预警与风险曲线

    def issue_warning(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "warning.write")
        notice = WarningNotice.from_dict(raw)
        latest = self.connection.execute(
            "SELECT version FROM warnings WHERE warning_id=? ORDER BY version DESC LIMIT 1",
            (notice.warning_id,),
        ).fetchone()
        expected = 1 if latest is None else int(latest["version"]) + 1
        if notice.version != expected:
            raise Conflict(f"预警版本必须接续最新版本 {expected}")
        content_sha256 = digest(raw)
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO warnings(warning_id,version,issued_at,commit_deadline_at,earliest_return_at,"
                "level,hazard,affected_villages_json,content_sha256,revision_of,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    notice.warning_id,
                    notice.version,
                    notice.issued_at,
                    notice.commit_deadline_at,
                    notice.earliest_return_at,
                    notice.level,
                    notice.hazard,
                    canonical_json(list(notice.affected_villages)),
                    content_sha256,
                    None if latest is None else int(latest["version"]),
                    actor_id,
                    self._now(),
                ),
            )
            for point in notice.curve:
                self.connection.execute(
                    "INSERT INTO warning_curve_points(warning_id,version,seq,observed_at,risk_level,note) "
                    "VALUES(?,?,?,?,?,?)",
                    (notice.warning_id, notice.version, point["seq"], point["observed_at"], point["risk_level"], point["note"]),
                )
            self._audit("warning", notice.warning_id, "warning.issued", actor_id, {
                "version": notice.version,
                "level": notice.level,
                "villages": list(notice.affected_villages),
                "curve_points": len(notice.curve),
            })
        return {
            "warning_id": notice.warning_id,
            "version": notice.version,
            "state": "issued",
            "curve_points": len(notice.curve),
            "sha256": content_sha256,
        }

    def warning(self, warning_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM warnings WHERE warning_id=? ORDER BY version DESC LIMIT 1",
            (warning_id,),
        ).fetchone()
        if row is None:
            raise NotFound("预警不存在")
        points = self.connection.execute(
            "SELECT seq,observed_at,risk_level,note FROM warning_curve_points "
            "WHERE warning_id=? AND version=? ORDER BY seq",
            (warning_id, row["version"]),
        ).fetchall()
        return {
            "warning_id": row["warning_id"],
            "version": row["version"],
            "issued_at": row["issued_at"],
            "commit_deadline_at": row["commit_deadline_at"],
            "earliest_return_at": row["earliest_return_at"],
            "level": row["level"],
            "hazard": row["hazard"],
            "affected_villages": json.loads(row["affected_villages_json"]),
            "risk_curve": [dict(point) for point in points],
            "revision_of": row["revision_of"],
        }

    # ------------------------------------------------------------------ 人口基线与资源

    def register_village(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "baseline.write")
        village_id = raw.get("village_id")
        if not isinstance(village_id, str) or not village_id.strip():
            raise ValidationFailed("village_id 不能为空")
        village_id = village_id.strip()
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValidationFailed("name 不能为空")
        assembly = str(raw.get("assembly_point", "") or "").strip()
        contact = str(raw.get("contact", "") or "").strip()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO villages(village_id,name,assembly_point,contact,created_at) VALUES(?,?,?,?,?)",
                    (village_id, name.strip(), assembly, contact, self._now()),
                )
                self._audit("village", village_id, "village.registered", actor_id, {"name": name.strip()})
        except sqlite3.IntegrityError as exc:
            raise Conflict("行政村已经存在") from exc
        return {"village_id": village_id, "state": "registered"}

    def upsert_household(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "baseline.write")
        record = HouseholdRecord.from_dict(raw)
        if self.connection.execute("SELECT 1 FROM villages WHERE village_id=?", (record.village_id,)).fetchone() is None:
            raise NotFound("行政村不存在")
        existing = self.connection.execute(
            "SELECT revision FROM households WHERE household_id=?", (record.household_id,)
        ).fetchone()
        with transaction(self.connection, immediate=True):
            if existing is None:
                self.connection.execute(
                    "INSERT INTO households(household_id,village_id,head_name,members,mobility_impaired,"
                    "school_children,special_medical,dangerous_house,assembly_point,transport_required,"
                    "revision,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)",
                    (
                        record.household_id, record.village_id, record.head_name, record.members,
                        record.mobility_impaired, record.school_children, record.special_medical,
                        1 if record.dangerous_house else 0, record.assembly_point,
                        1 if record.transport_required else 0, actor_id, self._now(),
                    ),
                )
                event = "household.registered"
                revision = 1
            else:
                if self.connection.execute(
                    "SELECT 1 FROM plan_batches WHERE household_id=?", (record.household_id,)
                ).fetchone() is not None:
                    raise InvalidState("家庭已进入转移方案，基线已快照，不能修改")
                self.connection.execute(
                    "UPDATE households SET village_id=?,head_name=?,members=?,mobility_impaired=?,"
                    "school_children=?,special_medical=?,dangerous_house=?,assembly_point=?,"
                    "transport_required=?,revision=revision+1 WHERE household_id=?",
                    (
                        record.village_id, record.head_name, record.members, record.mobility_impaired,
                        record.school_children, record.special_medical,
                        1 if record.dangerous_house else 0, record.assembly_point,
                        1 if record.transport_required else 0, record.household_id,
                    ),
                )
                event = "household.rebased"
                revision = int(existing["revision"]) + 1
            self._audit("household", record.household_id, event, actor_id, {
                "village_id": record.village_id,
                "members": record.members,
                "vulnerable": record.vulnerable,
                "dangerous_house": record.dangerous_house,
            })
        return {"household_id": record.household_id, "state": "registered", "revision": revision}

    def register_vehicle(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "baseline.write")
        record = VehicleRecord.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO vehicles(vehicle_id,plate,seats,wheelchair_seats,active,created_at) "
                    "VALUES(?,?,?,?,1,?)",
                    (record.vehicle_id, record.plate, record.seats, record.wheelchair_seats, self._now()),
                )
                self._audit("vehicle", record.vehicle_id, "vehicle.registered", actor_id, {
                    "seats": record.seats,
                    "wheelchair_seats": record.wheelchair_seats,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("车辆编号已经存在") from exc
        return {"vehicle_id": record.vehicle_id, "state": "registered"}

    def register_shelter(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "baseline.write")
        record = ShelterRecord.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO shelters(shelter_id,name,address,beds_total,medical_beds_total,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (record.shelter_id, record.name, record.address, record.beds_total, record.medical_beds_total, self._now()),
                )
                self._audit("shelter", record.shelter_id, "shelter.registered", actor_id, {
                    "beds_total": record.beds_total,
                    "medical_beds_total": record.medical_beds_total,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("临时住所编号已经存在") from exc
        return {"shelter_id": record.shelter_id, "state": "registered"}

    def register_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "baseline.write")
        record = RouteRecord.from_dict(raw)
        if self.connection.execute("SELECT 1 FROM villages WHERE village_id=?", (record.village_id,)).fetchone() is None:
            raise NotFound("行政村不存在")
        if self.connection.execute("SELECT 1 FROM shelters WHERE shelter_id=?", (record.shelter_id,)).fetchone() is None:
            raise NotFound("临时住所不存在")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO evac_routes(route_id,village_id,shelter_id,minutes,throughput_per_hour,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (record.route_id, record.village_id, record.shelter_id, record.minutes, record.throughput_per_hour, self._now()),
                )
                self._audit("route", record.route_id, "route.registered", actor_id, {
                    "village_id": record.village_id,
                    "shelter_id": record.shelter_id,
                    "minutes": record.minutes,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("疏散道路编号已经存在") from exc
        return {"route_id": record.route_id, "state": "registered"}

    def report_road_status(self, actor_id: str, route_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "route_status.write")
        if self.connection.execute("SELECT 1 FROM evac_routes WHERE route_id=?", (route_id,)).fetchone() is None:
            raise NotFound("疏散道路不存在")
        state = str(raw.get("state", "")).strip()
        if state not in ("open", "restricted", "closed"):
            raise ValidationFailed("state 必须是 open、restricted 或 closed")
        effective_at = raw.get("effective_at", self._now())
        if not isinstance(effective_at, str):
            raise ValidationFailed("effective_at 必须是 ISO 8601 时间")
        try:
            effective_at = parse_utc(effective_at, "effective_at").isoformat().replace("+00:00", "Z")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        note = str(raw.get("note", "") or "").strip()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO route_status_events(route_id,effective_at,state,note,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (route_id, effective_at, state, note, actor_id, self._now()),
            )
            event_id = int(cursor.lastrowid)
            self._audit("route", route_id, "route.status_reported", actor_id, {
                "event_id": event_id,
                "state": state,
                "effective_at": effective_at,
            })
        return {"event_id": event_id, "route_id": route_id, "state": state, "effective_at": effective_at}

    # ------------------------------------------------------------------ 候选组合与确认

    def _latest_warning_row(self, warning_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM warnings WHERE warning_id=? ORDER BY version DESC LIMIT 1",
            (warning_id,),
        ).fetchone()
        if row is None:
            raise NotFound("预警不存在")
        return row

    def _warning_mapping(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "warning_id": row["warning_id"],
            "version": row["version"],
            "issued_at": row["issued_at"],
            "commit_deadline_at": row["commit_deadline_at"],
            "earliest_return_at": row["earliest_return_at"],
            "level": row["level"],
            "affected_villages": json.loads(row["affected_villages_json"]),
        }

    def _households(self, village_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = tuple(dict.fromkeys(village_ids))
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"SELECT * FROM households WHERE village_id IN ({marks}) ORDER BY household_id",
            ids,
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["vulnerable"] = bool(
                row["mobility_impaired"] or row["school_children"] or row["special_medical"]
            )
            result.append(item)
        return result

    def _planning_inputs(self, warning_row: sqlite3.Row, *, exclude_households=()):
        warning = self._warning_mapping(warning_row)
        villages = warning["affected_villages"]
        missing = [village for village in villages
                   if self.connection.execute("SELECT 1 FROM villages WHERE village_id=?", (village,)).fetchone() is None]
        if missing:
            raise InvalidState(f"预警涉及的行政村尚未登记基线: {', '.join(missing)}")
        households = [h for h in self._households(villages) if h["household_id"] not in exclude_households]
        vehicles = [dict(row) for row in self.connection.execute(
            "SELECT vehicle_id,plate,seats,wheelchair_seats,active FROM vehicles WHERE active=1 ORDER BY vehicle_id"
        ).fetchall()]
        shelters = [dict(row) for row in self.connection.execute(
            "SELECT shelter_id,name,address,beds_total,medical_beds_total FROM shelters ORDER BY shelter_id"
        ).fetchall()]
        routes = [dict(row) for row in self.connection.execute(
            "SELECT route_id,village_id,shelter_id,minutes,throughput_per_hour FROM evac_routes ORDER BY route_id"
        ).fetchall()]
        route_events = [dict(row) for row in self.connection.execute(
            "SELECT event_id,route_id,effective_at,state FROM route_status_events ORDER BY event_id"
        ).fetchall()]
        return warning, households, vehicles, shelters, routes, route_events

    def _external_windows(
        self,
        *,
        exclude_plan: str | None = None,
        prefix_batches=(),
        hold_end: str | None = None,
    ):
        """其他已冻结方案的资源占用，加上本方案已执行阶段的不可释放占用。"""
        vehicle_windows = []
        shelter_windows = []
        for row in self.connection.execute(
            "SELECT plan_id,vehicle_id,window_start,window_end FROM plan_vehicle_usage",
        ).fetchall():
            if exclude_plan is not None and row["plan_id"] == exclude_plan:
                continue
            vehicle_windows.append(dict(row))
        for row in self.connection.execute(
            "SELECT plan_id,shelter_id,beds,medical_beds,window_start,window_end FROM plan_shelter_usage",
        ).fetchall():
            if exclude_plan is not None and row["plan_id"] == exclude_plan:
                continue
            shelter_windows.append(dict(row))
        # 修订时，已执行阶段（终态已锁定）的资源占用与外部冻结方案同等保护：
        # 车辆至少占用一个往返周期，床位从到达一直占用到最新返迁窗口。
        for batch in prefix_batches:
            if batch.get("vehicle_id") and batch.get("depart_at"):
                minutes_row = self.connection.execute(
                    "SELECT minutes FROM evac_routes WHERE route_id=?", (batch["route_id"],)
                ).fetchone()
                end = batch["arrive_at"]
                if minutes_row is not None:
                    end = (parse_utc(batch["depart_at"], "depart_at")
                           + timedelta(minutes=2 * int(minutes_row["minutes"]))
                           ).isoformat().replace("+00:00", "Z")
                vehicle_windows.append({
                    "plan_id": exclude_plan,
                    "vehicle_id": batch["vehicle_id"],
                    "window_start": batch["depart_at"],
                    "window_end": end,
                })
            if batch.get("shelter_id") and batch.get("arrive_at"):
                shelter_windows.append({
                    "plan_id": exclude_plan,
                    "shelter_id": batch["shelter_id"],
                    "beds": batch["beds"],
                    "medical_beds": batch["medical_beds"],
                    "window_start": batch["arrive_at"],
                    "window_end": hold_end or batch["arrive_at"],
                })
        return vehicle_windows, shelter_windows

    def generate_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plan.generate")
        plan_id = str(raw.get("plan_id", "") or "").strip()
        warning_id = str(raw.get("warning_id", "") or "").strip()
        if not plan_id or not warning_id:
            raise ValidationFailed("plan_id 和 warning_id 不能为空")
        evacuation_start = raw.get("evacuation_start")
        if not isinstance(evacuation_start, str):
            raise ValidationFailed("evacuation_start 必须是 ISO 8601 时间")
        try:
            evacuation_start = parse_utc(evacuation_start, "evacuation_start").isoformat().replace("+00:00", "Z")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        warning_row = self._latest_warning_row(warning_id)
        if self.connection.execute("SELECT 1 FROM plans WHERE plan_id=?", (plan_id,)).fetchone() is not None:
            raise Conflict("方案编号已经存在")
        warning, households, vehicles, shelters, routes, route_events = self._planning_inputs(warning_row)
        if not households:
            raise InvalidState("影响村没有可转移家庭基线")
        if parse_utc(evacuation_start, "evacuation_start") >= parse_utc(warning_row["commit_deadline_at"], "deadline"):
            raise ValidationFailed("撤离开始时间必须早于承诺转移截止时间")
        vehicle_windows, shelter_windows = self._external_windows()
        candidates = generate_candidates(
            warning,
            households,
            vehicles,
            shelters,
            routes,
            route_events,
            evacuation_start=evacuation_start,
            external_vehicle_windows=vehicle_windows,
            external_shelter_windows=shelter_windows,
        )
        content_sha256 = digest({"warning_version": warning["version"], "candidates": candidates})
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO plans(plan_id,warning_id,warning_version,evacuation_start,stage_interval_minutes,"
                "state,candidates_json,content_sha256,revision,generated_by,generated_at,created_at) "
                "VALUES(?,?,?,?,?,'draft',?,?,1,?,?,?)",
                (
                    plan_id, warning_id, warning["version"], evacuation_start, 60,
                    canonical_json(candidates), content_sha256, actor_id, self._now(), self._now(),
                ),
            )
            self.connection.execute(
                "INSERT INTO plan_revisions(plan_id,revision,warning_version,candidates_json,generated_by,generated_at) "
                "VALUES(?,1,?,?,?,?)",
                (plan_id, warning["version"], canonical_json(candidates), actor_id, self._now()),
            )
            self._audit("plan", plan_id, "plan.generated", actor_id, {
                "warning_id": warning_id,
                "warning_version": warning["version"],
                "candidates": [c["candidate_id"] for c in candidates],
                "feasible": [c["feasible"] for c in candidates],
            })
        return self.plan_summary(plan_id)

    def plan_summary(self, plan_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound("转移方案不存在")
        candidates = json.loads(row["candidates_json"])
        chosen = None
        if row["chosen_candidate_id"]:
            chosen = next((c for c in candidates if c["candidate_id"] == row["chosen_candidate_id"]), None)
        return {
            "plan_id": plan_id,
            "warning_id": row["warning_id"],
            "warning_version": row["warning_version"],
            "evacuation_start": row["evacuation_start"],
            "state": row["state"],
            "revision": row["revision"],
            "chosen_candidate_id": row["chosen_candidate_id"],
            "confirmed_at": row["confirmed_at"],
            "candidates": [
                {
                    "candidate_id": c["candidate_id"],
                    "label": c["label"],
                    "feasible": c["feasible"],
                    "violations": c["violations"],
                    "totals": c["totals"],
                    "stages": [
                        {"sequence": s["sequence"], "scheduled_depart_at": s["scheduled_depart_at"],
                         "batches": len(s["batches"])}
                        for s in c["stages"]
                    ],
                    "return_plan": c["return_plan"],
                }
                for c in candidates
            ],
            "chosen": chosen,
        }

    def candidate_detail(self, plan_id: str, candidate_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT candidates_json FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound("转移方案不存在")
        candidates = json.loads(row["candidates_json"])
        candidate = next((c for c in candidates if c["candidate_id"] == candidate_id), None)
        if candidate is None:
            raise NotFound("候选组合不存在")
        return {"plan_id": plan_id, **candidate}

    def _release_unfinalized(self, plan_id: str) -> None:
        """修订等待重新确认期间，释放尚未执行阶段占用的车辆与床位窗口。

        已经开始执行（有现场回执）的阶段整阶段冻结；未开始阶段的全部
        批次与资源窗口删除，等待按新预警重新规划。
        """
        frozen = self._frozen_stages(plan_id)
        stage_rows = self.connection.execute(
            "SELECT stage_id,sequence FROM plan_stages WHERE plan_id=?", (plan_id,)
        ).fetchall()
        for stage_row in stage_rows:
            if int(stage_row["sequence"]) in frozen:
                self.connection.execute(
                    "UPDATE plan_stages SET finalized=1 WHERE stage_id=?", (stage_row["stage_id"],)
                )
                continue
            self.connection.execute(
                "DELETE FROM plan_vehicle_usage WHERE batch_id IN "
                "(SELECT batch_id FROM plan_batches WHERE stage_id=?)",
                (stage_row["stage_id"],)
            )
            self.connection.execute("DELETE FROM plan_batches WHERE stage_id=?", (stage_row["stage_id"],))
            self.connection.execute("DELETE FROM plan_stages WHERE stage_id=?", (stage_row["stage_id"],))
        # 床位窗口缩减为已冻结阶段的实际占用。
        prefix = self._frozen_prefix(plan_id)
        self.connection.execute("DELETE FROM plan_shelter_usage WHERE plan_id=?", (plan_id,))
        warning_end = self.connection.execute(
            "SELECT w.earliest_return_at FROM plans p JOIN warnings w "
            "ON w.warning_id=p.warning_id AND w.version=p.warning_version WHERE p.plan_id=?",
            (plan_id,),
        ).fetchone()
        hold_end = warning_end["earliest_return_at"] if warning_end else self._now()
        merged: dict[str, dict[str, Any]] = {}
        for batch in prefix:
            entry = merged.setdefault(batch["shelter_id"], {
                "beds": 0, "medical_beds": 0, "window_start": batch["arrive_at"],
            })
            entry["beds"] += batch["beds"]
            entry["medical_beds"] += batch["medical_beds"]
            if batch["arrive_at"] < entry["window_start"]:
                entry["window_start"] = batch["arrive_at"]
        for shelter_id, item in merged.items():
            self.connection.execute(
                "INSERT INTO plan_shelter_usage(plan_id,shelter_id,beds,medical_beds,window_start,window_end) "
                "VALUES(?,?,?,?,?,?)",
                (plan_id, shelter_id, item["beds"], item["medical_beds"], item["window_start"], hold_end),
            )

    def _frozen_stages(self, plan_id: str) -> set[int]:
        """已经开始执行（出现过任一现场回执）的阶段。"""
        rows = self.connection.execute(
            "SELECT DISTINCT s.sequence FROM plan_stages s "
            "JOIN plan_batches b ON b.stage_id=s.stage_id "
            "JOIN household_progress hp ON hp.plan_id=b.plan_id AND hp.household_id=b.household_id "
            "WHERE s.plan_id=?",
            (plan_id,),
        ).fetchall()
        return {int(row["sequence"]) for row in rows}

    def _frozen_prefix(self, plan_id: str) -> list[dict[str, Any]]:
        """已经开始执行（有现场回执）阶段的全部批次，整阶段冻结不参与重排。"""
        frozen = self._frozen_stages(plan_id)
        if not frozen:
            return []
        marks = ",".join("?" for _ in frozen)
        rows = self.connection.execute(
            f"SELECT b.*, s.sequence FROM plan_batches b JOIN plan_stages s ON s.stage_id=b.stage_id "
            f"WHERE b.plan_id=? AND s.sequence IN ({marks}) ORDER BY s.sequence,b.batch_id",
            (plan_id, *sorted(frozen)),
        ).fetchall()
        return [dict(row) for row in rows]

    def confirm_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plan.confirm")
        plan_id = str(raw.get("plan_id", "") or "").strip()
        candidate_id = str(raw.get("candidate_id", "") or "").strip()
        expected_revision = raw.get("expected_revision")
        idempotency_key = raw.get("idempotency_key")
        if not plan_id or not candidate_id:
            raise ValidationFailed("plan_id 和 candidate_id 不能为空")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ValidationFailed("expected_revision 必须是整数")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValidationFailed("idempotency_key 不能为空")
        idempotency_key = idempotency_key.strip()
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM evac_idempotency WHERE scope='plan.confirm' AND idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != digest(raw):
                raise Conflict("幂等键对应不同确认内容")
            return json.loads(stored["response_json"])
        row = self.connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound("转移方案不存在")
        if row["state"] != "draft":
            raise InvalidState("方案已经确认或关闭")
        if int(row["revision"]) != expected_revision:
            raise Conflict("方案已有修订，请基于最新候选组合重新确认")
        candidates = json.loads(row["candidates_json"])
        candidate = next((c for c in candidates if c["candidate_id"] == candidate_id), None)
        if candidate is None:
            raise NotFound("候选组合不存在")
        if not candidate["feasible"]:
            raise InvalidState(f"候选组合不满足硬约束: {', '.join(candidate['violations'])}")
        with transaction(self.connection, immediate=True):
            # 确认前基于最新道路事件与资源占用再核验一次候选可行性。
            warning_row = self.connection.execute(
                "SELECT * FROM warnings WHERE warning_id=? AND version=?",
                (row["warning_id"], row["warning_version"]),
            ).fetchone()
            warning, households, vehicles, shelters, routes, route_events = self._planning_inputs(warning_row)
            ext_vehicle, ext_shelter = self._external_windows()
            fresh = next((c for c in generate_candidates(
                warning, households, vehicles, shelters, routes, route_events,
                evacuation_start=row["evacuation_start"],
                external_vehicle_windows=ext_vehicle,
                external_shelter_windows=ext_shelter,
            ) if c["candidate_id"] == candidate_id), None)
            if fresh is None or not fresh["feasible"]:
                raise Conflict("资源情况已变化，候选组合不再可行，请重新生成方案")
            self._freeze_candidate(plan_id, fresh, row["warning_version"], [],
                                   hold_end=warning_row["earliest_return_at"])
            updated = self.connection.execute(
                "UPDATE plans SET state='confirmed',chosen_candidate_id=?,candidates_json=?,confirmed_by=?,"
                "confirmed_at=? WHERE plan_id=? AND state='draft' AND revision=?",
                (candidate_id, canonical_json(candidates), actor_id, self._now(), plan_id, expected_revision),
            )
            if updated.rowcount != 1:
                raise Conflict("方案状态已变化，确认失败")
            response = {"plan_id": plan_id, "state": "confirmed", "candidate_id": candidate_id}
            self.connection.execute(
                "INSERT INTO evac_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('plan.confirm',?,?,?,?)",
                (idempotency_key, digest(raw), canonical_json(response), self._now()),
            )
            self._audit("plan", plan_id, "plan.confirmed", actor_id, {
                "candidate_id": candidate_id,
                "revision": expected_revision,
                "trips": len(fresh["vehicle_trips"]),
                "shelters": len(fresh["shelter_reservations"]),
                "people": fresh["totals"]["people"],
            })
        return response

    def _freeze_candidate(self, plan_id: str, candidate: Mapping[str, Any], warning_version: int,
                         prefix: list[dict[str, Any]], *, hold_end: str, plan_revision: int = 1) -> None:
        """一次性写入阶段、批次、车辆车次窗口和住所床位窗口。"""
        finalized_sequences = {batch["sequence"] for batch in prefix}
        # 修订后重新确认：清掉未执行阶段的旧快照，保留已执行阶段。
        old_stages = self.connection.execute(
            "SELECT stage_id FROM plan_stages WHERE plan_id=?", (plan_id,)
        ).fetchall()
        for stage_row in old_stages:
            if self.connection.execute(
                "SELECT 1 FROM plan_stages WHERE stage_id=? AND finalized=1", (stage_row["stage_id"],)
            ).fetchone():
                continue
            self.connection.execute("DELETE FROM plan_vehicle_usage WHERE batch_id IN "
                                    "(SELECT batch_id FROM plan_batches WHERE stage_id=?)", (stage_row["stage_id"],))
            self.connection.execute("DELETE FROM plan_batches WHERE stage_id=?", (stage_row["stage_id"],))
            self.connection.execute("DELETE FROM plan_stages WHERE stage_id=?", (stage_row["stage_id"],))
        self.connection.execute("DELETE FROM plan_shelter_usage WHERE plan_id=?", (plan_id,))
        next_sequence = max(finalized_sequences, default=0)
        for stage in candidate["stages"]:
            sequence = next_sequence + int(stage["sequence"])
            cursor = self.connection.execute(
                "INSERT INTO plan_stages(plan_id,sequence,scheduled_depart_at,warning_version,plan_revision,"
                "state,finalized) VALUES(?,?,?,?,?,'planned',0)",
                (plan_id, sequence, stage["scheduled_depart_at"], warning_version, plan_revision),
            )
            stage_id = int(cursor.lastrowid)
            for batch in stage["batches"]:
                if not batch.get("assigned"):
                    continue
                self._insert_batch(plan_id, stage_id, sequence, batch)
        self._rebuild_shelter_usage(plan_id, candidate["shelter_reservations"], prefix, hold_end)

    def _insert_batch(self, plan_id: str, stage_id: int, sequence: int, batch: Mapping[str, Any]) -> int:
        cursor = self.connection.execute(
            "INSERT INTO plan_batches(stage_id,plan_id,household_id,village_id,headcount,vulnerable,"
            "route_id,vehicle_id,shelter_id,beds,medical_beds,assembly_point,depart_at,arrive_at,state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                stage_id, plan_id, batch["household_id"], batch["village_id"], batch["headcount"],
                1 if batch["vulnerable"] else 0, batch.get("route_id"), batch.get("vehicle_id"),
                batch.get("shelter_id"), batch["beds"], batch["medical_beds"],
                batch.get("assembly_point", ""), batch.get("depart_at"), batch.get("arrive_at"), "planned",
            ),
        )
        batch_id = int(cursor.lastrowid)
        if batch.get("vehicle_id") and batch.get("depart_at"):
            arrive = batch["arrive_at"]
            minutes_row = self.connection.execute(
                "SELECT minutes FROM evac_routes WHERE route_id=?", (batch["route_id"],)
            ).fetchone()
            window_end = arrive
            if minutes_row is not None:
                window_end = (parse_utc(batch["depart_at"], "depart_at")
                              + timedelta(minutes=2 * int(minutes_row["minutes"]))
                              ).isoformat().replace("+00:00", "Z")
            vehicle = self.connection.execute(
                "SELECT seats,wheelchair_seats FROM vehicles WHERE vehicle_id=?", (batch["vehicle_id"],)
            ).fetchone()
            self.connection.execute(
                "INSERT INTO plan_vehicle_usage(plan_id,batch_id,vehicle_id,seats,wheelchair_seats,"
                "window_start,window_end) VALUES(?,?,?,?,?,?,?)",
                (plan_id, batch_id, batch["vehicle_id"], vehicle["seats"], vehicle["wheelchair_seats"],
                 batch["depart_at"], window_end),
            )
        return batch_id

    def _rebuild_shelter_usage(self, plan_id: str, reservations, prefix: list[dict[str, Any]], hold_end: str) -> None:
        window_end = hold_end
        merged: dict[str, dict[str, int]] = {}
        sources = list(reservations)
        for batch in prefix:
            sources.append({
                "shelter_id": batch["shelter_id"],
                "beds": batch["beds"],
                "medical_beds": batch["medical_beds"],
                "window_start": batch["arrive_at"],
            })
        for item in sources:
            entry = merged.setdefault(item["shelter_id"], {"beds": 0, "medical_beds": 0, "window_start": item.get("window_start", window_end)})
            entry["beds"] += int(item["beds"])
            entry["medical_beds"] += int(item["medical_beds"])
            if item.get("window_start") and item["window_start"] < entry["window_start"]:
                entry["window_start"] = item["window_start"]
        for shelter_id, item in merged.items():
            self.connection.execute(
                "INSERT INTO plan_shelter_usage(plan_id,shelter_id,beds,medical_beds,window_start,window_end) "
                "VALUES(?,?,?,?,?,?)",
                (plan_id, shelter_id, item["beds"], item["medical_beds"], item["window_start"], window_end),
            )

    # ------------------------------------------------------------------ 预警修订

    def revise_for_warning(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """预警修订后重算候选组合；只影响尚未执行（未冻结）的阶段。"""
        self._require(actor_id, "plan.revise")
        plan_id = str(raw.get("plan_id", "") or "").strip()
        expected_revision = raw.get("expected_revision")
        if not plan_id:
            raise ValidationFailed("plan_id 不能为空")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ValidationFailed("expected_revision 必须是整数")
        row = self.connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound("转移方案不存在")
        if row["state"] not in ("confirmed", "executing"):
            raise InvalidState("只有已确认且执行中的方案可以随预警修订")
        if int(row["revision"]) != expected_revision:
            raise Conflict("方案修订版本不匹配")
        warning_row = self._latest_warning_row(row["warning_id"])
        if int(warning_row["version"]) <= int(row["warning_version"]):
            raise InvalidState("没有更新的预警版本")
        prefix = self._frozen_prefix(plan_id)
        frozen_households = {batch["household_id"] for batch in prefix}
        warning, households, vehicles, shelters, routes, route_events = self._planning_inputs(
            warning_row, exclude_households=frozen_households
        )
        revision_start = raw.get("evacuation_start", self._now())
        try:
            revision_start = parse_utc(revision_start, "evacuation_start").isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError) as exc:
            raise ValidationFailed(str(exc)) from exc
        if parse_utc(revision_start, "evacuation_start") >= parse_utc(warning_row["commit_deadline_at"], "deadline"):
            raise ValidationFailed("修订阶段的撤离开始时间必须早于新的承诺截止时间")
        ext_vehicle, ext_shelter = self._external_windows(
            exclude_plan=plan_id, prefix_batches=prefix, hold_end=warning_row["earliest_return_at"]
        )
        candidates = generate_candidates(
            warning,
            households,
            vehicles,
            shelters,
            routes,
            route_events,
            evacuation_start=revision_start,
            external_vehicle_windows=ext_vehicle,
            external_shelter_windows=ext_shelter,
        )
        # 已执行阶段作为只读前缀拼接到每个新候选组合。
        stage_lookups = {}
        for batch in prefix:
            stage_lookups.setdefault(batch["sequence"], []).append(batch)
        prefix_stages = []
        for sequence in sorted(stage_lookups):
            batches = stage_lookups[sequence]
            prefix_stages.append({
                "sequence": sequence,
                "scheduled_depart_at": batches[0]["depart_at"],
                "warning_version": self.connection.execute(
                    "SELECT warning_version FROM plan_stages WHERE plan_id=? AND sequence=?",
                    (plan_id, sequence),
                ).fetchone()["warning_version"],
                "frozen": True,
                "batches": [
                    {
                        "household_id": b["household_id"],
                        "village_id": b["village_id"],
                        "headcount": b["headcount"],
                        "vulnerable": bool(b["vulnerable"]),
                        "dangerous_house": False,
                        "beds": b["beds"],
                        "medical_beds": b["medical_beds"],
                        "assembly_point": b["assembly_point"],
                        "transport_required": b["vehicle_id"] is not None,
                        "assigned": True,
                        "frozen": True,
                        "route_id": b["route_id"],
                        "vehicle_id": b["vehicle_id"],
                        "shelter_id": b["shelter_id"],
                        "depart_at": b["depart_at"],
                        "arrive_at": b["arrive_at"],
                    }
                    for b in batches
                ],
            })
        no_return_rows = self.connection.execute(
            "SELECT household_id FROM household_progress WHERE plan_id=? AND event_type IN ('exempt','returned')",
            (plan_id,),
        ).fetchall()
        no_return = {row["household_id"] for row in no_return_rows}
        for candidate in candidates:
            offset = len(prefix_stages)
            for stage in candidate["stages"]:
                stage["sequence"] += offset
            candidate["stages"] = prefix_stages + candidate["stages"]
            candidate["return_plan"] = merge_return_plan(
                prefix,
                candidate,
                warning_row["earliest_return_at"],
                int(candidate["interval_minutes"]),
                skip_return=no_return,
            )
        content_sha256 = digest({"warning_version": warning["version"], "candidates": candidates})
        new_revision = int(row["revision"]) + 1
        with transaction(self.connection, immediate=True):
            # 释放上一版本尚未执行阶段的资源快照，已执行阶段保持冻结。
            self._release_unfinalized(plan_id)
            self.connection.execute(
                "UPDATE plans SET warning_version=?,evacuation_start=?,candidates_json=?,content_sha256=?,"
                "revision=?,chosen_candidate_id=NULL,generated_by=?,generated_at=?,state='executing' "
                "WHERE plan_id=? AND revision=?",
                (warning["version"], revision_start, canonical_json(candidates), content_sha256, new_revision,
                 actor_id, self._now(), plan_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO plan_revisions(plan_id,revision,warning_version,candidates_json,generated_by,generated_at) "
                "VALUES(?,?,?,?,?,?)",
                (plan_id, new_revision, warning["version"], canonical_json(candidates), actor_id, self._now()),
            )
            self._audit("plan", plan_id, "plan.revised", actor_id, {
                "warning_version": warning["version"],
                "revision": new_revision,
                "frozen_stages": len(prefix_stages),
                "replanned_households": len(households),
                "feasible": [c["feasible"] for c in candidates],
            })
        return self.plan_summary(plan_id)

    def reconfirm_revision(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """修订后的候选组合经负责人再次确认，冻结未执行阶段的资源。"""
        self._require(actor_id, "plan.confirm")
        plan_id = str(raw.get("plan_id", "") or "").strip()
        candidate_id = str(raw.get("candidate_id", "") or "").strip()
        expected_revision = raw.get("expected_revision")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ValidationFailed("expected_revision 必须是整数")
        row = self.connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound("转移方案不存在")
        if row["state"] not in ("confirmed", "executing"):
            raise InvalidState("方案当前状态不允许确认修订")
        if int(row["revision"]) != expected_revision:
            raise Conflict("方案修订版本不匹配")
        candidates = json.loads(row["candidates_json"])
        candidate = next((c for c in candidates if c["candidate_id"] == candidate_id), None)
        if candidate is None:
            raise NotFound("候选组合不存在")
        if not candidate["feasible"]:
            raise InvalidState(f"候选组合不满足硬约束: {', '.join(candidate['violations'])}")
        prefix = self._frozen_prefix(plan_id)
        frozen_households = {batch["household_id"] for batch in prefix}
        warning_row = self.connection.execute(
            "SELECT * FROM warnings WHERE warning_id=? AND version=?",
            (row["warning_id"], row["warning_version"]),
        ).fetchone()
        warning, households, vehicles, shelters, routes, route_events = self._planning_inputs(
            warning_row, exclude_households=frozen_households
        )
        ext_vehicle, ext_shelter = self._external_windows(
            exclude_plan=plan_id, prefix_batches=prefix, hold_end=warning_row["earliest_return_at"]
        )
        fresh = next((c for c in generate_candidates(
            warning, households, vehicles, shelters, routes, route_events,
            evacuation_start=row["evacuation_start"],
            external_vehicle_windows=ext_vehicle,
            external_shelter_windows=ext_shelter,
        ) if c["candidate_id"] == candidate_id), None)
        if fresh is None or not fresh["feasible"]:
            raise Conflict("资源情况已变化，修订候选组合不再可行")
        with transaction(self.connection, immediate=True):
            self._freeze_candidate(plan_id, fresh, row["warning_version"], prefix,
                                   hold_end=warning_row["earliest_return_at"],
                                   plan_revision=expected_revision)
            self.connection.execute(
                "UPDATE plans SET chosen_candidate_id=?,confirmed_by=?,confirmed_at=? "
                "WHERE plan_id=? AND revision=?",
                (candidate_id, actor_id, self._now(), plan_id, expected_revision),
            )
            self._audit("plan", plan_id, "plan.revision_confirmed", actor_id, {
                "candidate_id": candidate_id,
                "revision": expected_revision,
            })
        return {"plan_id": plan_id, "state": "confirmed", "candidate_id": candidate_id, "revision": expected_revision}

    # ------------------------------------------------------------------ 现场回执

    def record_receipt(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "receipt.write")
        plan_id = str(raw.get("plan_id", "") or "").strip()
        household_id = str(raw.get("household_id", "") or "").strip()
        event_type = str(raw.get("event_type", "") or "").strip()
        if event_type not in RECEIPT_EVENTS:
            raise ValidationFailed("event_type 必须是 notified、departed、arrived、exempt 或 returned")
        observed_at = raw.get("observed_at")
        if not isinstance(observed_at, str):
            raise ValidationFailed("observed_at 必须是 ISO 8601 时间")
        try:
            observed_at = parse_utc(observed_at, "observed_at").isoformat().replace("+00:00", "Z")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        headcount = raw.get("headcount")
        if headcount is not None:
            if isinstance(headcount, bool) or not isinstance(headcount, int) or headcount < 0:
                raise ValidationFailed("headcount 必须是非负整数")
        reporter = str(raw.get("reporter", actor_id) or actor_id).strip()
        note = str(raw.get("note", "") or "").strip()
        idempotency_key = raw.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValidationFailed("idempotency_key 不能为空")
        idempotency_key = idempotency_key.strip()

        plan = self.connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None:
            raise NotFound("转移方案不存在")
        if plan["state"] not in ("confirmed", "executing", "completed"):
            raise InvalidState("方案尚未确认，不能记录现场回执")
        batch = self.connection.execute(
            "SELECT b.*,s.sequence,s.finalized,s.stage_id FROM plan_batches b "
            "JOIN plan_stages s ON s.stage_id=b.stage_id "
            "WHERE b.plan_id=? AND b.household_id=?",
            (plan_id, household_id),
        ).fetchone()
        if batch is None:
            raise NotFound("家庭不在已冻结方案中")

        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM evac_idempotency WHERE scope='receipt' AND idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != digest(raw):
                raise Conflict("幂等键对应不同回执内容")
            return json.loads(stored["response_json"])

        with transaction(self.connection, immediate=True):
            try:
                cursor = self.connection.execute(
                    "INSERT INTO receipts(plan_id,household_id,event_type,observed_at,headcount,reporter,note,"
                    "idempotency_key,received_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (plan_id, household_id, event_type, observed_at, headcount, reporter, note,
                     idempotency_key, self._now()),
                )
            except sqlite3.IntegrityError:
                # 乱序重发的同事件回执：归并为同一条现场事实。
                receipt_id = self.connection.execute(
                    "SELECT receipt_id FROM receipts WHERE plan_id=? AND household_id=? AND event_type=? AND observed_at=?",
                    (plan_id, household_id, event_type, observed_at),
                ).fetchone()["receipt_id"]
                duplicate = True
            else:
                receipt_id = int(cursor.lastrowid)
                duplicate = False
            applied = self._merge_progress(plan_id, household_id, event_type, observed_at, headcount)
            if applied:
                self.connection.execute(
                    "UPDATE plan_batches SET state=? WHERE plan_id=? AND household_id=?",
                    (BATCH_STATE[event_type], plan_id, household_id),
                )
                self._refresh_stage_state(batch["stage_id"])
                if not batch["finalized"]:
                    self.connection.execute(
                        "UPDATE plan_stages SET finalized=1 WHERE stage_id=?", (batch["stage_id"],)
                    )
                self.connection.execute(
                    "UPDATE plans SET state='executing' WHERE plan_id=? AND state='confirmed'",
                    (plan_id,),
                )
                self._maybe_complete(plan_id)
            response = {
                "plan_id": plan_id,
                "household_id": household_id,
                "receipt_id": receipt_id,
                "event_type": event_type,
                "observed_at": observed_at,
                "duplicate": duplicate,
                "applied": applied,
            }
            progress_row = self.connection.execute(
                "SELECT event_type,observed_at,terminal FROM household_progress "
                "WHERE plan_id=? AND household_id=?", (plan_id, household_id)
            ).fetchone()
            response["current_state"] = dict(progress_row) if progress_row is not None else None
            self.connection.execute(
                "INSERT INTO evac_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('receipt',?,?,?,?)",
                (idempotency_key, digest(raw), canonical_json(response), self._now()),
            )
            self._audit("receipt", f"{plan_id}:{household_id}", "receipt.recorded", actor_id, {
                "receipt_id": receipt_id,
                "event_type": event_type,
                "observed_at": observed_at,
                "duplicate": duplicate,
                "applied": applied,
            })
        return response

    def _merge_progress(
        self,
        plan_id: str,
        household_id: str,
        event_type: str,
        observed_at: str,
        headcount: int | None,
    ) -> bool:
        """按事件时间归并；保护已确认终态，回执更早或终态已锁定则不改写。"""
        current = self.connection.execute(
            "SELECT event_type,observed_at FROM household_progress WHERE plan_id=? AND household_id=?",
            (plan_id, household_id),
        ).fetchone()
        terminal = event_type in TERMINAL_EVENTS
        if current is not None:
            current_terminal = current["event_type"] in TERMINAL_EVENTS
            if current_terminal and event_type not in TERMINAL_EVENTS:
                return False  # 终态保护：已安全到达/豁免后，中途状态不再回退
            if observed_at < current["observed_at"]:
                return False  # 乱序旧事件
            if observed_at == current["observed_at"] and EVENT_RANK[event_type] <= EVENT_RANK[current["event_type"]]:
                return False
        self.connection.execute(
            "INSERT INTO household_progress(plan_id,household_id,event_type,observed_at,headcount,terminal,updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(plan_id,household_id) DO UPDATE SET "
            "event_type=excluded.event_type,observed_at=excluded.observed_at,headcount=excluded.headcount,"
            "terminal=excluded.terminal,updated_at=excluded.updated_at",
            (plan_id, household_id, event_type, observed_at, headcount, 1 if terminal else 0, self._now()),
        )
        return True

    def _refresh_stage_state(self, stage_id: int) -> None:
        rows = self.connection.execute(
            "SELECT state FROM plan_batches WHERE stage_id=?", (stage_id,)
        ).fetchall()
        if not rows:
            return
        states = {row["state"] for row in rows}
        active = {"notified", "in_transit"}
        terminal = {"delivered", "exempt", "returned"}
        if states & active:
            new_state = "executing"
        elif states <= terminal:
            new_state = "done"
        else:
            new_state = "planned"
        self.connection.execute("UPDATE plan_stages SET state=? WHERE stage_id=?", (new_state, stage_id))

    def _maybe_complete(self, plan_id: str) -> None:
        chosen = self.connection.execute(
            "SELECT chosen_candidate_id FROM plans WHERE plan_id=?", (plan_id,)
        ).fetchone()["chosen_candidate_id"]
        if not chosen:
            # 修订后尚未重新确认时不判定完成，避免误关单。
            return
        row = self.connection.execute(
            "SELECT COUNT(*) total, SUM(CASE WHEN hp.terminal=1 THEN 1 ELSE 0 END) done "
            "FROM plan_batches b LEFT JOIN household_progress hp "
            "ON hp.plan_id=b.plan_id AND hp.household_id=b.household_id WHERE b.plan_id=?",
            (plan_id,),
        ).fetchone()
        if row["total"] and int(row["total"]) == int(row["done"] or 0):
            self.connection.execute("UPDATE plans SET state='completed' WHERE plan_id=?", (plan_id,))

    # ------------------------------------------------------------------ 解释查询

    def explain_plan(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        plan = self.connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None:
            raise NotFound("转移方案不存在")
        warning = self.warning(plan["warning_id"])
        warning = {k: v for k, v in warning.items() if k != "risk_curve"}
        warning["applied_version"] = plan["warning_version"]
        warning["latest_version"] = self.connection.execute(
            "SELECT MAX(version) v FROM warnings WHERE warning_id=?", (plan["warning_id"],)
        ).fetchone()["v"]

        batches = self.connection.execute(
            "SELECT b.*,s.sequence stage_sequence,s.state stage_state,s.finalized "
            "FROM plan_batches b JOIN plan_stages s ON s.stage_id=b.stage_id "
            "WHERE b.plan_id=? ORDER BY s.sequence,b.batch_id",
            (plan_id,),
        ).fetchall()
        candidates = json.loads(plan["candidates_json"])
        unassigned: list[dict[str, Any]] = []
        chosen_id = plan["chosen_candidate_id"]
        if chosen_id:
            chosen = next((c for c in candidates if c["candidate_id"] == chosen_id), None)
            if chosen is not None:
                assigned_ids = {batch["household_id"] for batch in batches}
                for stage in chosen["stages"]:
                    for batch in stage["batches"]:
                        if not batch.get("assigned") and batch["household_id"] not in assigned_ids:
                            unassigned.append({
                                "household_id": batch["household_id"],
                                "village_id": batch["village_id"],
                                "headcount": batch["headcount"],
                                "reason": batch.get("reason", "unassigned"),
                                "stage": stage["sequence"],
                            })

        def _impact(village_id: str) -> dict[str, Any]:
            return village_stats.setdefault(village_id, {
                "village_id": village_id,
                "planned_people": 0,
                "evacuated_people": 0,
                "returned_people": 0,
                "exempt_people": 0,
                "pending_people": 0,
                "unassigned_people": 0,
                "households_planned": 0,
                "households_done": 0,
            })

        evacuated = 0
        returned = 0
        exempted = 0
        in_transit = 0
        village_stats: dict[str, dict[str, Any]] = {}
        pending: list[dict[str, Any]] = []
        for item in unassigned:
            stats = _impact(item["village_id"])
            stats["planned_people"] += item["headcount"]
            stats["unassigned_people"] += item["headcount"]
            stats["households_planned"] += 1
        for batch in batches:
            stats = _impact(batch["village_id"])
            stats["planned_people"] += batch["headcount"]
            stats["households_planned"] += 1
            progress = self.connection.execute(
                "SELECT * FROM household_progress WHERE plan_id=? AND household_id=?",
                (plan_id, batch["household_id"]),
            ).fetchone()
            actual = batch["headcount"]
            if progress is not None and progress["headcount"] is not None:
                actual = progress["headcount"]
            if progress is None:
                stats["pending_people"] += actual
                pending.append({
                    "household_id": batch["household_id"],
                    "village_id": batch["village_id"],
                    "headcount": batch["headcount"],
                    "reason": "not_started",
                    "stage": batch["stage_sequence"],
                })
                continue
            event = progress["event_type"]
            if event in ("arrived",):
                evacuated += actual
                stats["evacuated_people"] += actual
                stats["households_done"] += 1
            elif event == "returned":
                evacuated += actual
                returned += actual
                stats["evacuated_people"] += actual
                stats["returned_people"] += actual
                stats["households_done"] += 1
            elif event == "exempt":
                exempted += actual
                stats["exempt_people"] += actual
                stats["households_done"] += 1
            elif event in ("notified", "departed"):
                in_transit += actual
                stats["pending_people"] += actual
                pending.append({
                    "household_id": batch["household_id"],
                    "village_id": batch["village_id"],
                    "headcount": batch["headcount"],
                    "reason": "in_transit" if event == "departed" else "notified_pending_departure",
                    "stage": batch["stage_sequence"],
                    "last_event": event,
                    "last_event_at": progress["observed_at"],
                })

        stages = self.connection.execute(
            "SELECT sequence,scheduled_depart_at,warning_version,state,finalized FROM plan_stages "
            "WHERE plan_id=? ORDER BY sequence",
            (plan_id,),
        ).fetchall()
        receipts = self.connection.execute(
            "SELECT event_type,COUNT(*) count FROM receipts WHERE plan_id=? GROUP BY event_type",
            (plan_id,),
        ).fetchall()

        chosen_summary = None
        if chosen_id:
            chosen = next((c for c in candidates if c["candidate_id"] == chosen_id), None)
            if chosen is not None:
                chosen_summary = {
                    "candidate_id": chosen_id,
                    "return_plan": chosen["return_plan"],
                }
        returned_receipts = self.connection.execute(
            "SELECT hp.household_id,b.village_id,hp.observed_at FROM household_progress hp "
            "JOIN plan_batches b ON b.plan_id=hp.plan_id AND b.household_id=hp.household_id "
            "WHERE hp.plan_id=? AND hp.event_type='returned' ORDER BY hp.observed_at",
            (plan_id,),
        ).fetchall()

        return {
            "plan_id": plan_id,
            "state": plan["state"],
            "revision": plan["revision"],
            "warning": warning,
            "chosen_candidate_id": chosen_id,
            "people": {
                "planned": sum(s["planned_people"] for s in village_stats.values()),
                "actually_evacuated": evacuated,
                "returned": returned,
                "exempt": exempted,
                "in_transit_or_notified": in_transit,
            },
            "stages": [dict(row) for row in stages],
            "village_impact": [village_stats[key] for key in sorted(village_stats)],
            "unfinished": pending + unassigned,
            "return_plan": None if chosen_summary is None else chosen_summary["return_plan"],
            "returned_actual": [dict(row) for row in returned_receipts],
            "receipt_counts": {row["event_type"]: row["count"] for row in receipts},
        }

    def road_status(self, route_id: str, at: str | None = None) -> dict[str, Any]:
        if self.connection.execute("SELECT 1 FROM evac_routes WHERE route_id=?", (route_id,)).fetchone() is None:
            raise NotFound("疏散道路不存在")
        at = at or self._now()
        events = [dict(row) for row in self.connection.execute(
            "SELECT event_id,route_id,effective_at,state FROM route_status_events WHERE route_id=? ORDER BY event_id",
            (route_id,),
        ).fetchall()]
        return {"route_id": route_id, "as_of": at, "state": route_state_at(events, at), "events": events}
