"""应急转移协同领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9一-鿿][A-Za-z0-9_.::一-鿿-]{0,63}$")
WARNING_LEVELS = ("blue", "yellow", "orange", "red")
ROUTE_STATES = ("open", "restricted", "closed")
RECEIPT_EVENTS = ("notified", "departed", "arrived", "exempt", "returned")
TERMINAL_EVENTS = frozenset({"arrived", "exempt", "returned"})


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def optional_text(value: object, field: str, maximum: int = 256) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationFailed(f"{field} 必须是文本")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def integer(value: object, field: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationFailed(f"{field} 必须是整数")
    if minimum is not None and value < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and value > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return value


def timestamp(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        return parse_utc(text, field).isoformat().replace("+00:00", "Z")
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class WarningNotice:
    warning_id: str
    version: int
    issued_at: str
    commit_deadline_at: str
    earliest_return_at: str
    level: str
    hazard: str
    affected_villages: tuple[str, ...]
    curve: tuple[dict[str, Any], ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WarningNotice":
        warning_id = identifier(raw.get("warning_id"), "warning_id")
        version = integer(raw.get("version", 1), "version", minimum=1)
        level = required_text(raw.get("level"), "level", 16).lower()
        if level not in WARNING_LEVELS:
            raise ValidationFailed("level 必须是 blue、yellow、orange 或 red")
        issued_at = timestamp(raw.get("issued_at"), "issued_at")
        deadline = timestamp(raw.get("commit_deadline_at"), "commit_deadline_at")
        earliest_return = timestamp(raw.get("earliest_return_at"), "earliest_return_at")
        if deadline <= issued_at:
            raise ValidationFailed("commit_deadline_at 必须晚于 issued_at")
        if earliest_return <= deadline:
            raise ValidationFailed("earliest_return_at 必须晚于 commit_deadline_at")
        villages = raw.get("affected_villages", [])
        if not isinstance(villages, list) or not villages:
            raise ValidationFailed("affected_villages 必须是非空数组")
        affected = tuple(identifier(item, "affected_villages") for item in villages)
        if len(set(affected)) != len(affected):
            raise ValidationFailed("affected_villages 不能重复")
        curve_raw = raw.get("risk_curve", [])
        if not isinstance(curve_raw, list) or not curve_raw:
            raise ValidationFailed("risk_curve 必须是非空数组")
        curve: list[dict[str, Any]] = []
        last_time = ""
        for index, point in enumerate(curve_raw, start=1):
            if not isinstance(point, Mapping):
                raise ValidationFailed("risk_curve 每一项必须是对象")
            point_level = required_text(point.get("risk_level"), f"risk_curve[{index}].risk_level", 16).lower()
            if point_level not in WARNING_LEVELS:
                raise ValidationFailed(f"risk_curve[{index}].risk_level 非法")
            observed_at = timestamp(point.get("observed_at"), f"risk_curve[{index}].observed_at")
            if observed_at < issued_at:
                raise ValidationFailed(f"risk_curve[{index}].observed_at 不能早于 issued_at")
            if last_time and observed_at < last_time:
                raise ValidationFailed("risk_curve 必须按 observed_at 升序")
            last_time = observed_at
            curve.append({
                "seq": index,
                "observed_at": observed_at,
                "risk_level": point_level,
                "note": optional_text(point.get("note"), f"risk_curve[{index}].note", 200),
            })
        return cls(
            warning_id=warning_id,
            version=version,
            issued_at=issued_at,
            commit_deadline_at=deadline,
            earliest_return_at=earliest_return,
            level=level,
            hazard=required_text(raw.get("hazard"), "hazard", 200),
            affected_villages=affected,
            curve=tuple(curve),
        )


@dataclass(frozen=True, slots=True)
class HouseholdRecord:
    household_id: str
    village_id: str
    head_name: str
    members: int
    mobility_impaired: int
    school_children: int
    special_medical: int
    dangerous_house: bool
    assembly_point: str
    transport_required: bool

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HouseholdRecord":
        members = integer(raw.get("members"), "members", minimum=1, maximum=999)
        mobility = integer(raw.get("mobility_impaired", 0), "mobility_impaired", minimum=0, maximum=members)
        children = integer(raw.get("school_children", 0), "school_children", minimum=0, maximum=members)
        medical = integer(raw.get("special_medical", 0), "special_medical", minimum=0, maximum=members)
        dangerous = bool(raw.get("dangerous_house"))
        if not isinstance(raw.get("dangerous_house", False), bool):
            raise ValidationFailed("dangerous_house 必须是布尔值")
        transport = raw.get("transport_required", True)
        if not isinstance(transport, bool):
            raise ValidationFailed("transport_required 必须是布尔值")
        return cls(
            household_id=identifier(raw.get("household_id"), "household_id"),
            village_id=identifier(raw.get("village_id"), "village_id"),
            head_name=required_text(raw.get("head_name"), "head_name", 64),
            members=members,
            mobility_impaired=mobility,
            school_children=children,
            special_medical=medical,
            dangerous_house=dangerous,
            assembly_point=optional_text(raw.get("assembly_point"), "assembly_point", 200),
            transport_required=transport,
        )

    @property
    def vulnerable(self) -> bool:
        return bool(self.mobility_impaired or self.school_children or self.special_medical)


@dataclass(frozen=True, slots=True)
class VehicleRecord:
    vehicle_id: str
    plate: str
    seats: int
    wheelchair_seats: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "VehicleRecord":
        seats = integer(raw.get("seats"), "seats", minimum=1, maximum=200)
        wheelchair = integer(raw.get("wheelchair_seats", 0), "wheelchair_seats", minimum=0, maximum=seats)
        return cls(
            vehicle_id=identifier(raw.get("vehicle_id"), "vehicle_id"),
            plate=required_text(raw.get("plate"), "plate", 32),
            seats=seats,
            wheelchair_seats=wheelchair,
        )


@dataclass(frozen=True, slots=True)
class ShelterRecord:
    shelter_id: str
    name: str
    address: str
    beds_total: int
    medical_beds_total: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ShelterRecord":
        beds = integer(raw.get("beds_total"), "beds_total", minimum=1, maximum=100000)
        medical = integer(raw.get("medical_beds_total", 0), "medical_beds_total", minimum=0, maximum=beds)
        return cls(
            shelter_id=identifier(raw.get("shelter_id"), "shelter_id"),
            name=required_text(raw.get("name"), "name", 128),
            address=optional_text(raw.get("address"), "address", 256),
            beds_total=beds,
            medical_beds_total=medical,
        )


@dataclass(frozen=True, slots=True)
class RouteRecord:
    route_id: str
    village_id: str
    shelter_id: str
    minutes: int
    throughput_per_hour: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RouteRecord":
        return cls(
            route_id=identifier(raw.get("route_id"), "route_id"),
            village_id=identifier(raw.get("village_id"), "village_id"),
            shelter_id=identifier(raw.get("shelter_id"), "shelter_id"),
            minutes=integer(raw.get("minutes"), "minutes", minimum=1, maximum=600),
            throughput_per_hour=integer(raw.get("throughput_per_hour"), "throughput_per_hour", minimum=1, maximum=100000),
        )
