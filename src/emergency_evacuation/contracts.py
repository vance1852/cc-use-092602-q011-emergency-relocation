"""应急转移协同领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

# 重点人群保护类别：行动不便（残障/高龄）、在校儿童、特殊医疗需求，另含孕幼。
VULNERABLE_KINDS = {"mobility", "children", "medical", "infant"}
RISK_LEVELS = {"blue", "yellow", "orange", "red"}
ROAD_STATES = {"open", "restricted", "closed"}
TRANSFERABLE_KINDS = {"vehicle", "ambulance", "walk", "shelter", "relative"}
DESTINATION_KINDS = {"shelter", "relative", "medical"}


def required_text(value: object, field_name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field_name} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field_name} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field_name: str) -> str:
    result = required_text(value, field_name, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field_name} 格式不正确")
    return result


def non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationFailed(f"{field_name} 必须是非负整数")
    return value


def positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field_name} 必须是正整数")
    return value


def timestamp(value: object, field_name: str) -> str:
    text = required_text(value, field_name, 40)
    try:
        return parse_utc(text, field_name).isoformat().replace("+00:00", "Z")
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def _vulnerable(raw: object) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationFailed("vulnerable 必须是数组")
    result: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValidationFailed(f"vulnerable[{index}] 必须是对象")
        kind = required_text(item.get("kind"), f"vulnerable[{index}].kind", 16).lower()
        if kind not in VULNERABLE_KINDS:
            raise ValidationFailed(f"vulnerable[{index}].kind 不受支持")
        note = item.get("note", "")
        if not isinstance(note, str) or len(note) > 128:
            raise ValidationFailed(f"vulnerable[{index}].note 必须是短文本")
        result.append({"kind": kind, "note": note.strip()})
    return result


@dataclass(frozen=True, slots=True)
class WarningRevision:
    """一次预警版本（含风险变化曲线观测点）。"""

    warning_id: str
    revision: int
    level: str
    issued_at: str
    deadline_at: str
    village_ids: tuple[str, ...]
    curve_points: tuple[dict[str, Any], ...]
    notes: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WarningRevision":
        warning_id = identifier(raw.get("warning_id"), "warning_id")
        revision = positive_int(raw.get("revision", 1), "revision")
        level = required_text(raw.get("level"), "level", 16).lower()
        if level not in RISK_LEVELS:
            raise ValidationFailed("level 必须是 blue、yellow、orange 或 red")
        issued_at = timestamp(raw.get("issued_at"), "issued_at")
        deadline_at = timestamp(raw.get("deadline_at"), "deadline_at")
        if deadline_at <= issued_at:
            raise ValidationFailed("deadline_at 必须晚于 issued_at")
        villages = raw.get("village_ids")
        if not isinstance(villages, list) or not villages:
            raise ValidationFailed("village_ids 必须是非空数组")
        village_ids = tuple(identifier(item, "village_ids 项") for item in villages)
        if len(set(village_ids)) != len(village_ids):
            raise ValidationFailed("village_ids 不能重复")
        points = raw.get("risk_curve", [])
        if not isinstance(points, list) or not points:
            raise ValidationFailed("risk_curve 必须是非空数组")
        curve: list[dict[str, Any]] = []
        previous_at = ""
        for index, item in enumerate(points):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"risk_curve[{index}] 必须是对象")
            observed_at = timestamp(item.get("observed_at"), f"risk_curve[{index}].observed_at")
            score = item.get("risk_score")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise ValidationFailed(f"risk_curve[{index}].risk_score 必须是数值")
            if not 0 <= float(score) <= 100:
                raise ValidationFailed(f"risk_curve[{index}].risk_score 必须在 0 到 100 之间")
            if observed_at < previous_at:
                raise ValidationFailed("risk_curve 观测点必须按时间升序")
            previous_at = observed_at
            curve.append({"observed_at": observed_at, "risk_score": float(score)})
        notes = raw.get("notes", "")
        if not isinstance(notes, str) or len(notes) > 512:
            raise ValidationFailed("notes 必须是短文本")
        return cls(warning_id, revision, level, issued_at, deadline_at, village_ids, tuple(curve), notes.strip())


@dataclass(frozen=True, slots=True)
class VillageBaseline:
    village_id: str
    name: str
    households: int
    population: int
    at_risk_households: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "VillageBaseline":
        households = non_negative_int(raw.get("households"), "households")
        population = non_negative_int(raw.get("population"), "population")
        at_risk = non_negative_int(raw.get("at_risk_households"), "at_risk_households")
        if at_risk > households:
            raise ValidationFailed("at_risk_households 不能超过 households")
        return cls(
            identifier(raw.get("village_id"), "village_id"),
            required_text(raw.get("name"), "name"),
            households,
            population,
            at_risk,
        )


@dataclass(frozen=True, slots=True)
class HouseholdProfile:
    household_id: str
    village_id: str
    members: int
    vulnerable: tuple[dict[str, str], ...]
    transferable: bool
    transfer_kind: str
    needs_ambulance: bool
    shelter_required: bool
    family_assembly_point: str
    medical_destination_id: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HouseholdProfile":
        members = positive_int(raw.get("members"), "members")
        vulnerable = tuple(_vulnerable(raw.get("vulnerable")))
        if len(vulnerable) > members:
            raise ValidationFailed("vulnerable 人数不能超过 members")
        transferable = raw.get("transferable", True)
        if not isinstance(transferable, bool):
            raise ValidationFailed("transferable 必须是布尔值")
        transfer_kind = required_text(raw.get("transfer_kind", "vehicle"), "transfer_kind", 16).lower()
        if transfer_kind not in TRANSFERABLE_KINDS:
            raise ValidationFailed("transfer_kind 必须是 vehicle、ambulance、walk、shelter 或 relative")
        needs_ambulance = any(item["kind"] == "medical" for item in vulnerable)
        assembly = raw.get("family_assembly_point", "")
        if not isinstance(assembly, str) or len(assembly) > 128:
            raise ValidationFailed("family_assembly_point 必须是短文本")
        medical_destination = raw.get("medical_destination_id") or ""
        if medical_destination:
            medical_destination = identifier(medical_destination, "medical_destination_id")
        shelter_required = raw.get("shelter_required", transfer_kind in {"vehicle", "ambulance", "walk", "shelter"})
        if not isinstance(shelter_required, bool):
            raise ValidationFailed("shelter_required 必须是布尔值")
        return cls(
            identifier(raw.get("household_id"), "household_id"),
            identifier(raw.get("village_id"), "village_id"),
            members,
            vulnerable,
            transferable,
            transfer_kind,
            needs_ambulance,
            shelter_required,
            assembly.strip(),
            medical_destination,
        )


@dataclass(frozen=True, slots=True)
class Vehicle:
    vehicle_id: str
    kind: str
    seats: int
    ambulance: bool
    seats_for_mobility: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Vehicle":
        kind = required_text(raw.get("kind", "bus"), "kind", 24)
        seats = positive_int(raw.get("seats"), "seats")
        ambulance = raw.get("ambulance", False)
        if not isinstance(ambulance, bool):
            raise ValidationFailed("ambulance 必须是布尔值")
        mobility_seats = non_negative_int(raw.get("seats_for_mobility", 0), "seats_for_mobility")
        if mobility_seats > seats:
            raise ValidationFailed("seats_for_mobility 不能超过 seats")
        return cls(identifier(raw.get("vehicle_id"), "vehicle_id"), kind, seats, ambulance, mobility_seats)


@dataclass(frozen=True, slots=True)
class Shelter:
    shelter_id: str
    name: str
    village_id: str
    beds: int
    accessible_beds: int
    medical_beds: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Shelter":
        beds = positive_int(raw.get("beds"), "beds")
        accessible = non_negative_int(raw.get("accessible_beds", 0), "accessible_beds")
        medical = non_negative_int(raw.get("medical_beds", 0), "medical_beds")
        if accessible + medical > beds:
            raise ValidationFailed("accessible_beds 与 medical_beds 之和不能超过 beds")
        return cls(
            identifier(raw.get("shelter_id"), "shelter_id"),
            required_text(raw.get("name"), "name"),
            identifier(raw.get("village_id"), "village_id"),
            beds,
            accessible,
            medical,
        )


@dataclass(frozen=True, slots=True)
class RoadSegment:
    road_id: str
    village_id: str
    state: str
    detour_minutes: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RoadSegment":
        state = required_text(raw.get("state", "open"), "state", 16).lower()
        if state not in ROAD_STATES:
            raise ValidationFailed("state 必须是 open、restricted 或 closed")
        detour = non_negative_int(raw.get("detour_minutes", 0), "detour_minutes")
        return cls(
            identifier(raw.get("road_id"), "road_id"),
            identifier(raw.get("village_id"), "village_id"),
            state,
            detour if state == "restricted" else 0,
        )


@dataclass(frozen=True, slots=True)
class CandidateOptions:
    """生成候选组合时的可调参数（均有保守默认值）。"""

    batch_minutes: int = field(default=90)
    return_window_hours: int = field(default=72)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "CandidateOptions":
        raw = raw or {}
        batch_minutes = positive_int(raw.get("batch_minutes", 90), "batch_minutes")
        return_window = positive_int(raw.get("return_window_hours", 72), "return_window_hours")
        return cls(batch_minutes, return_window)
