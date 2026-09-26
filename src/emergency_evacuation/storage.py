"""应急转移协同服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS evac_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','commander','field','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS warnings (
    warning_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    level TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    village_ids_json TEXT NOT NULL,
    risk_curve_json TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','superseded','stood_down')),
    recorded_by TEXT NOT NULL REFERENCES evac_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(warning_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_warnings_series
ON warnings(warning_id, revision);

CREATE TABLE IF NOT EXISTS village_baselines (
    village_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    households INTEGER NOT NULL,
    population INTEGER NOT NULL,
    at_risk_households INTEGER NOT NULL,
    revised_by TEXT NOT NULL REFERENCES evac_users(user_id),
    revised_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS households (
    household_id TEXT PRIMARY KEY,
    village_id TEXT NOT NULL REFERENCES village_baselines(village_id),
    members INTEGER NOT NULL,
    vulnerable_json TEXT NOT NULL DEFAULT '[]',
    transferable INTEGER NOT NULL DEFAULT 1 CHECK(transferable IN (0,1)),
    transfer_kind TEXT NOT NULL,
    needs_ambulance INTEGER NOT NULL DEFAULT 0 CHECK(needs_ambulance IN (0,1)),
    shelter_required INTEGER NOT NULL DEFAULT 1 CHECK(shelter_required IN (0,1)),
    family_assembly_point TEXT NOT NULL DEFAULT '',
    medical_destination_id TEXT NOT NULL DEFAULT '',
    revised_by TEXT NOT NULL REFERENCES evac_users(user_id),
    revised_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_households_village
ON households(village_id, household_id);

CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    seats INTEGER NOT NULL,
    ambulance INTEGER NOT NULL DEFAULT 0 CHECK(ambulance IN (0,1)),
    seats_for_mobility INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES evac_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shelters (
    shelter_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    village_id TEXT NOT NULL REFERENCES village_baselines(village_id),
    beds INTEGER NOT NULL,
    accessible_beds INTEGER NOT NULL DEFAULT 0,
    medical_beds INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES evac_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS road_segments (
    road_id TEXT NOT NULL,
    village_id TEXT NOT NULL REFERENCES village_baselines(village_id),
    state TEXT NOT NULL CHECK(state IN ('open','restricted','closed')),
    detour_minutes INTEGER NOT NULL DEFAULT 0,
    updated_by TEXT NOT NULL REFERENCES evac_users(user_id),
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(road_id, village_id)
);

CREATE TABLE IF NOT EXISTS evacuation_plans (
    plan_id TEXT PRIMARY KEY,
    warning_id TEXT NOT NULL,
    warning_revision INTEGER NOT NULL,
    candidate_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'proposed'
        CHECK(state IN ('proposed','confirmed','frozen','superseded','cancelled')),
    based_on_plan_id TEXT REFERENCES evacuation_plans(plan_id),
    confirmed_by TEXT REFERENCES evac_users(user_id),
    confirmed_at TEXT,
    frozen_by TEXT REFERENCES evac_users(user_id),
    frozen_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES evac_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(warning_id, warning_revision, input_sha256)
);

CREATE TABLE IF NOT EXISTS frozen_resources (
    plan_id TEXT NOT NULL REFERENCES evacuation_plans(plan_id),
    resource_kind TEXT NOT NULL CHECK(resource_kind IN ('vehicle','shelter')),
    resource_id TEXT NOT NULL,
    reserved_seats INTEGER NOT NULL DEFAULT 0,
    reserved_mobility INTEGER NOT NULL DEFAULT 0,
    reserved_beds INTEGER NOT NULL DEFAULT 0,
    reserved_accessible INTEGER NOT NULL DEFAULT 0,
    reserved_medical INTEGER NOT NULL DEFAULT 0,
    frozen_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, resource_kind, resource_id)
);

CREATE TABLE IF NOT EXISTS field_receipts (
    receipt_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL DEFAULT 0,
    plan_id TEXT NOT NULL REFERENCES evacuation_plans(plan_id),
    household_id TEXT NOT NULL,
    batch_no INTEGER,
    vehicle_id TEXT,
    shelter_id TEXT,
    event_type TEXT NOT NULL
        CHECK(event_type IN ('notified','departed','arrived','sheltered','returned','absent','refused','medical_hold')),
    event_at TEXT NOT NULL,
    persons INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL REFERENCES evac_users(user_id),
    received_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, receipt_id)
);

CREATE INDEX IF NOT EXISTS idx_receipts_household_time
ON field_receipts(plan_id, household_id, event_at, event_seq);

-- 每个计划的终态受保护：终态事件只允许被同为终态的更晚事件更新。
CREATE TABLE IF NOT EXISTS household_progress (
    plan_id TEXT NOT NULL REFERENCES evacuation_plans(plan_id),
    household_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'pending','notified','departed','arrived','sheltered','returned','absent','refused','medical_hold')),
    persons INTEGER NOT NULL,
    event_at TEXT NOT NULL,
    batch_no INTEGER,
    vehicle_id TEXT,
    shelter_id TEXT,
    note TEXT NOT NULL DEFAULT '',
    updated_seq INTEGER NOT NULL DEFAULT 0,
    terminal INTEGER NOT NULL DEFAULT 0 CHECK(terminal IN (0,1)),
    PRIMARY KEY(plan_id, household_id)
);

CREATE TABLE IF NOT EXISTS return_plans (
    plan_id TEXT PRIMARY KEY REFERENCES evacuation_plans(plan_id),
    earliest_return_at TEXT NOT NULL,
    return_by_at TEXT NOT NULL,
    village_ids_json TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES evac_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evac_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS evac_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evac_audit_entity
ON evac_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10,
                                 check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
