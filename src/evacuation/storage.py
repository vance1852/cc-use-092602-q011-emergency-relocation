"""应急转移服务的 SQLite 模式和事务辅助。"""

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
    role TEXT NOT NULL CHECK(role IN ('risk','planner','director','dispatcher','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS warnings (
    warning_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    issued_at TEXT NOT NULL,
    commit_deadline_at TEXT NOT NULL,
    earliest_return_at TEXT NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('blue','yellow','orange','red')),
    hazard TEXT NOT NULL,
    affected_villages_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    revision_of INTEGER,
    created_by TEXT NOT NULL REFERENCES evac_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(warning_id, version)
);

CREATE TABLE IF NOT EXISTS warning_curve_points (
    point_id INTEGER PRIMARY KEY AUTOINCREMENT,
    warning_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    risk_level TEXT NOT NULL CHECK(risk_level IN ('blue','yellow','orange','red')),
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(warning_id, version, seq),
    FOREIGN KEY(warning_id, version) REFERENCES warnings(warning_id, version)
);

CREATE INDEX IF NOT EXISTS idx_curve_warning
ON warning_curve_points(warning_id, version, observed_at);

CREATE TABLE IF NOT EXISTS villages (
    village_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    assembly_point TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS households (
    household_id TEXT PRIMARY KEY,
    village_id TEXT NOT NULL REFERENCES villages(village_id),
    head_name TEXT NOT NULL,
    members INTEGER NOT NULL CHECK(members > 0),
    mobility_impaired INTEGER NOT NULL DEFAULT 0 CHECK(mobility_impaired >= 0),
    school_children INTEGER NOT NULL DEFAULT 0 CHECK(school_children >= 0),
    special_medical INTEGER NOT NULL DEFAULT 0 CHECK(special_medical >= 0),
    dangerous_house INTEGER NOT NULL CHECK(dangerous_house IN (0,1)),
    assembly_point TEXT NOT NULL DEFAULT '',
    transport_required INTEGER NOT NULL DEFAULT 1 CHECK(transport_required IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES evac_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_households_village
ON households(village_id, household_id);

CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id TEXT PRIMARY KEY,
    plate TEXT NOT NULL,
    seats INTEGER NOT NULL CHECK(seats > 0),
    wheelchair_seats INTEGER NOT NULL DEFAULT 0 CHECK(wheelchair_seats >= 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    CHECK(wheelchair_seats <= seats)
);

CREATE TABLE IF NOT EXISTS shelters (
    shelter_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    beds_total INTEGER NOT NULL CHECK(beds_total > 0),
    medical_beds_total INTEGER NOT NULL DEFAULT 0 CHECK(medical_beds_total >= 0),
    created_at TEXT NOT NULL,
    CHECK(medical_beds_total <= beds_total)
);

CREATE TABLE IF NOT EXISTS evac_routes (
    route_id TEXT PRIMARY KEY,
    village_id TEXT NOT NULL REFERENCES villages(village_id),
    shelter_id TEXT NOT NULL REFERENCES shelters(shelter_id),
    minutes INTEGER NOT NULL CHECK(minutes > 0),
    throughput_per_hour INTEGER NOT NULL CHECK(throughput_per_hour > 0),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_routes_village
ON evac_routes(village_id, shelter_id);

CREATE TABLE IF NOT EXISTS route_status_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES evac_routes(route_id),
    effective_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open','restricted','closed')),
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES evac_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_route_status_time
ON route_status_events(route_id, effective_at, event_id);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    warning_id TEXT NOT NULL,
    warning_version INTEGER NOT NULL,
    evacuation_start TEXT NOT NULL,
    stage_interval_minutes INTEGER NOT NULL CHECK(stage_interval_minutes > 0),
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK(state IN ('draft','confirmed','executing','completed','cancelled')),
    chosen_candidate_id TEXT,
    candidates_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    generated_by TEXT NOT NULL REFERENCES evac_users(user_id),
    generated_at TEXT NOT NULL,
    confirmed_by TEXT REFERENCES evac_users(user_id),
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(warning_id, warning_version) REFERENCES warnings(warning_id, version)
);

CREATE TABLE IF NOT EXISTS plan_revisions (
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    warning_version INTEGER NOT NULL,
    candidates_json TEXT NOT NULL,
    generated_by TEXT NOT NULL REFERENCES evac_users(user_id),
    generated_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, revision)
);

CREATE TABLE IF NOT EXISTS plan_stages (
    stage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    scheduled_depart_at TEXT NOT NULL,
    warning_version INTEGER NOT NULL,
    plan_revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'planned'
        CHECK(state IN ('planned','executing','done')),
    finalized INTEGER NOT NULL DEFAULT 0 CHECK(finalized IN (0,1)),
    UNIQUE(plan_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_stages_plan
ON plan_stages(plan_id, sequence);

CREATE TABLE IF NOT EXISTS plan_batches (
    batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_id INTEGER NOT NULL REFERENCES plan_stages(stage_id),
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    household_id TEXT NOT NULL REFERENCES households(household_id),
    village_id TEXT NOT NULL,
    headcount INTEGER NOT NULL CHECK(headcount > 0),
    vulnerable INTEGER NOT NULL CHECK(vulnerable IN (0,1)),
    route_id TEXT REFERENCES evac_routes(route_id),
    vehicle_id TEXT REFERENCES vehicles(vehicle_id),
    shelter_id TEXT REFERENCES shelters(shelter_id),
    beds INTEGER NOT NULL DEFAULT 0 CHECK(beds >= 0),
    medical_beds INTEGER NOT NULL DEFAULT 0 CHECK(medical_beds >= 0),
    assembly_point TEXT NOT NULL DEFAULT '',
    depart_at TEXT,
    arrive_at TEXT,
    state TEXT NOT NULL DEFAULT 'planned'
        CHECK(state IN ('planned','notified','in_transit','delivered','returned','exempt')),
    UNIQUE(plan_id, household_id)
);

CREATE INDEX IF NOT EXISTS idx_batches_stage
ON plan_batches(stage_id, batch_id);

CREATE TABLE IF NOT EXISTS plan_vehicle_usage (
    usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    batch_id INTEGER NOT NULL UNIQUE REFERENCES plan_batches(batch_id),
    vehicle_id TEXT NOT NULL REFERENCES vehicles(vehicle_id),
    seats INTEGER NOT NULL,
    wheelchair_seats INTEGER NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vehicle_usage_window
ON plan_vehicle_usage(vehicle_id, window_start, window_end);

CREATE TABLE IF NOT EXISTS plan_shelter_usage (
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    shelter_id TEXT NOT NULL REFERENCES shelters(shelter_id),
    beds INTEGER NOT NULL,
    medical_beds INTEGER NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    PRIMARY KEY(plan_id, shelter_id)
);

CREATE TABLE IF NOT EXISTS receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    household_id TEXT NOT NULL REFERENCES households(household_id),
    event_type TEXT NOT NULL CHECK(event_type IN ('notified','departed','arrived','exempt','returned')),
    observed_at TEXT NOT NULL,
    headcount INTEGER,
    reporter TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    received_at TEXT NOT NULL,
    UNIQUE(plan_id, household_id, event_type, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_receipts_household
ON receipts(plan_id, household_id, observed_at, receipt_id);

CREATE TABLE IF NOT EXISTS household_progress (
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    household_id TEXT NOT NULL REFERENCES households(household_id),
    event_type TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    headcount INTEGER,
    terminal INTEGER NOT NULL CHECK(terminal IN (0,1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, household_id)
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
    # ThreadingHTTPServer 会在工作线程中复用同一连接；WAL 与 IMMEDIATE
    # 事务配合 busy_timeout 保证跨线程写入安全。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
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
