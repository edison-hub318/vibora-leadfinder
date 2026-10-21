"""SQLite storage. One file, `leads.db`, shared by the CLI and the dashboard."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name_key TEXT NOT NULL UNIQUE,
    initials TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_login TEXT,
    daily_call_target INTEGER NOT NULL DEFAULT 40
);

CREATE TABLE IF NOT EXISTS login_attempts (
    name_key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    last_failure TEXT
);

CREATE TABLE IF NOT EXISTS towns (
    id INTEGER PRIMARY KEY,
    name_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    lat REAL,
    lng REAL,
    formatted_address TEXT,
    not_found INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    trade TEXT NOT NULL,
    town TEXT NOT NULL,
    radius_km REAL NOT NULL,
    requested INTEGER NOT NULL,
    queries_json TEXT NOT NULL DEFAULT '[]',
    raw_results INTEGER NOT NULL DEFAULT 0,
    qualified INTEGER NOT NULL DEFAULT 0,
    already_known INTEGER NOT NULL DEFAULT 0,
    requests_made INTEGER NOT NULL DEFAULT 0,
    est_cost_gbp REAL NOT NULL DEFAULT 0,
    funnel_json TEXT NOT NULL DEFAULT '{}',
    stop_reason TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    place_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    trade TEXT,
    town TEXT,
    address TEXT,
    postcode TEXT,
    phone TEXT,
    phone_e164 TEXT,
    phone_type TEXT,
    website TEXT,
    rating REAL,
    review_count INTEGER NOT NULL DEFAULT 0,
    business_status TEXT,
    primary_type TEXT,
    maps_url TEXT,
    lat REAL,
    lng REAL,
    status TEXT NOT NULL DEFAULT 'new',
    website_status TEXT,
    website_issues TEXT NOT NULL DEFAULT '[]',
    audit_json TEXT,
    pagespeed_score INTEGER,
    audited_at TEXT,
    existing_agency TEXT,
    ai_dated_score INTEGER,
    ai_verdict TEXT,
    ai_biggest_problem TEXT,
    hook TEXT,
    quality_flags TEXT NOT NULL DEFAULT '[]',
    entity_type TEXT NOT NULL DEFAULT 'unknown',
    entity_source TEXT,
    company_number TEXT,
    email TEXT,
    email_source TEXT,
    email_replied INTEGER NOT NULL DEFAULT 0,
    tps_status TEXT NOT NULL DEFAULT 'unchecked',
    tps_checked_at TEXT,
    tps_checked_by INTEGER,
    score INTEGER NOT NULL DEFAULT 0,
    owner_id INTEGER REFERENCES users(id),
    locked_by INTEGER REFERENCES users(id),
    locked_until TEXT,
    callback_at TEXT,
    setup_fee REAL,
    retainer REAL,
    lost_reason TEXT,
    stage_changed_at TEXT,
    consecutive_no_answer INTEGER NOT NULL DEFAULT 0,
    last_called_at TEXT,
    last_outcome TEXT,
    duplicate_of INTEGER REFERENCES leads(id),
    blocked_reason TEXT,
    preview_slug TEXT,
    preview_url TEXT,
    netlify_site_id TEXT,
    search_id INTEGER REFERENCES searches(id),
    first_seen TEXT NOT NULL,
    last_refreshed TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone_e164);
CREATE INDEX IF NOT EXISTS idx_leads_trade_town ON leads(trade, town);

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    user_id INTEGER REFERENCES users(id),
    outcome TEXT NOT NULL,
    note TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    callback_at TEXT,
    meeting_at TEXT,
    session_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_lead ON calls(lead_id);
CREATE INDEX IF NOT EXISTS idx_calls_created ON calls(created_at);

CREATE TABLE IF NOT EXISTS call_objections (
    id INTEGER PRIMARY KEY,
    call_id INTEGER NOT NULL REFERENCES calls(id),
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    user_id INTEGER REFERENCES users(id),
    tag TEXT NOT NULL,
    other_text TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS objection_notes (
    tag TEXT PRIMARY KEY,
    rebuttal TEXT NOT NULL DEFAULT '',
    updated_by INTEGER REFERENCES users(id),
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS stage_changes (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    user_id INTEGER REFERENCES users(id),
    from_stage TEXT,
    to_stage TEXT NOT NULL,
    setup_fee REAL,
    retainer REAL,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    user_id INTEGER REFERENCES users(id),
    call_id INTEGER REFERENCES calls(id),
    start_at TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL DEFAULT 30,
    status TEXT NOT NULL DEFAULT 'scheduled',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    kind TEXT NOT NULL DEFAULT 'initial',
    to_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_review',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    approved_by INTEGER REFERENCES users(id),
    approved_at TEXT,
    sent_by INTEGER REFERENCES users(id),
    sent_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_emails_lead ON emails(lead_id);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    country TEXT NOT NULL DEFAULT 'GB',
    filters_json TEXT NOT NULL DEFAULT '{}',
    template TEXT NOT NULL,
    followup_template TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    approved_by INTEGER REFERENCES users(id),
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS suppression (
    email TEXT PRIMARY KEY,
    reason TEXT,
    added_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dnc (
    id INTEGER PRIMARY KEY,
    phone_e164 TEXT NOT NULL UNIQUE,
    phone_raw TEXT,
    reason TEXT,
    lead_id INTEGER REFERENCES leads(id),
    user_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL,
    ts TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id),
    search_id INTEGER REFERENCES searches(id),
    est_cost_gbp REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_api_usage_sku_ts ON api_usage(sku, ts);

CREATE TABLE IF NOT EXISTS dial_sessions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    dials INTEGER NOT NULL DEFAULT 0,
    connects INTEGER NOT NULL DEFAULT 0,
    meetings INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS manual_entries (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    entry_type TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    minutes INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    entry_date TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    lead_id INTEGER REFERENCES leads(id),
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    tag TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_created ON activity(created_at);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class LFConnection(sqlite3.Connection):
    """sqlite3 connection that knows when it is inside `transaction()`, so helper
    functions can commit on their own when called alone but not inside a bigger unit."""

    tx_depth = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.tx_depth = 0


def commit(conn: sqlite3.Connection) -> None:
    """Commit unless we're inside an explicit `transaction()` block."""
    if getattr(conn, "tx_depth", 0) == 0:
        conn.commit()


# Columns added after the first release. `connect` adds any that an older leads.db is missing,
# so upgrading never needs a manual step.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("leads", "country", "TEXT NOT NULL DEFAULT 'GB'"),
    ("searches", "country", "TEXT NOT NULL DEFAULT 'GB'"),
    ("emails", "campaign_id", "INTEGER REFERENCES campaigns(id)"),
    ("emails", "bounced_at", "TEXT"),
    ("leads", "email_checked_at", "TEXT"),
]


def migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_campaign ON emails(campaign_id)")
    conn.commit()


def connect(path: Path | str, init: bool = True) -> sqlite3.Connection:
    """Open the database (creating the schema if needed, unless init=False)."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False, factory=LFConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        # WAL lets the dashboard and the CLI use the file at the same time.
        conn.execute("PRAGMA journal_mode = WAL")
    if init:
        conn.executescript(SCHEMA)
        migrate(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in a single transaction (commit on success, roll back on error)."""
    depth = getattr(conn, "tx_depth", 0)
    if depth > 0:
        conn.tx_depth = depth + 1  # type: ignore[attr-defined]
        try:
            yield conn
        finally:
            conn.tx_depth = depth  # type: ignore[attr-defined]
        return
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN")
    conn.tx_depth = 1  # type: ignore[attr-defined]
    try:
        yield conn
    except Exception:
        conn.tx_depth = 0  # type: ignore[attr-defined]
        conn.rollback()
        raise
    else:
        conn.tx_depth = 0  # type: ignore[attr-defined]
        conn.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for key in ("website_issues", "quality_flags"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except json.JSONDecodeError:
                d[key] = []
    return d


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return row["value"]


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO app_settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    commit(conn)
