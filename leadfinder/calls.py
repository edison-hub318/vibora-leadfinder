"""Logging calls, pipeline stages, objections, dial sessions and manual activity entries."""
from __future__ import annotations

import sqlite3
from datetime import datetime, time, timedelta
from typing import Any

from . import db
from .config import Settings
from .leads import LeadError, dnc_add, require_lead, unlock_lead
from .timeutil import LONDON, UTC, now_db, now_utc, to_db, today_london

OUTCOMES = {
    "no_answer": "No answer",
    "gatekeeper": "Gatekeeper",
    "connected": "Connected",
    "callback": "Callback",
    "meeting_booked": "Meeting booked",
    "not_interested": "Not interested",
    "wrong_number": "Wrong number",
}
# Outcomes where we actually spoke to the decision-maker.
CONNECT_OUTCOMES = ("connected", "meeting_booked", "not_interested")

STATUS_AFTER = {
    "no_answer": "called",
    "gatekeeper": "called",
    "connected": "interested",
    "callback": "callback",
    "meeting_booked": "meeting_booked",
    "not_interested": "not_interested",
    "wrong_number": "wrong_number",
}

STAGES = ("meeting_booked", "proposal_sent", "won", "lost")

DEFAULT_OBJECTIONS = [
    {"key": "too_expensive", "label": "Too expensive"},
    {"key": "use_socials", "label": "Use Facebook/socials instead"},
    {"key": "word_of_mouth", "label": "Word of mouth is enough"},
    {"key": "already_have_someone", "label": "Already have someone"},
    {"key": "bad_timing", "label": "Bad timing"},
    {"key": "not_decision_maker", "label": "Not the decision maker"},
    {"key": "no_value", "label": "Didn't see the value"},
    {"key": "other", "label": "Other"},
]
OBJECTION_OUTCOMES = ("not_interested", "callback", "gatekeeper")

MANUAL_TYPES = ["Door-knocking", "Social DMs", "Networking event", "Follow-up admin", "Site build", "Other"]


def objection_tags(conn: sqlite3.Connection) -> list[dict[str, str]]:
    tags = db.get_setting(conn, "objection_tags")
    return tags if isinstance(tags, list) and tags else DEFAULT_OBJECTIONS


def objection_label(conn: sqlite3.Connection, key: str) -> str:
    for t in objection_tags(conn):
        if t["key"] == key:
            return t["label"]
    return key.replace("_", " ").capitalize()


def meeting_minutes(conn: sqlite3.Connection) -> int:
    return int(db.get_setting(conn, "meeting_minutes", 30) or 30)


def default_callback() -> datetime:
    tomorrow = today_london() + timedelta(days=1)
    return datetime.combine(tomorrow, time(10, 0), tzinfo=LONDON).astimezone(UTC)


def log_call(
    conn: sqlite3.Connection,
    settings: Settings | None,
    lead_id: int,
    user_id: int | None,
    outcome: str,
    note: str | None = None,
    callback_at: datetime | None = None,
    meeting_at: datetime | None = None,
    duration_seconds: int = 0,
    objections: list[str] | None = None,
    objection_other: str | None = None,
    session_id: int | None = None,
) -> dict[str, Any]:
    """Record a call and move the lead on. Returns {'call_id', 'meeting_id', 'lead'}."""
    if outcome not in OUTCOMES:
        raise LeadError(f"Unknown outcome '{outcome}'. Use one of: {', '.join(OUTCOMES)}")
    lead = require_lead(conn, lead_id)
    if outcome == "meeting_booked" and meeting_at is None:
        raise LeadError("A meeting needs a date and time (e.g. --meeting 2026-10-20T14:00).")
    if outcome == "callback" and callback_at is None:
        callback_at = default_callback()
    duration_seconds = max(0, int(duration_seconds or 0))
    ended = now_utc()
    started = ended - timedelta(seconds=duration_seconds)
    valid_tags = {t["key"] for t in objection_tags(conn)}
    tags = [t for t in (objections or []) if t in valid_tags or t == "other"]

    with db.transaction(conn):
        cur = conn.execute(
            """INSERT INTO calls(lead_id, user_id, outcome, note, started_at, ended_at, duration_seconds,
                                 callback_at, meeting_at, session_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (lead_id, user_id, outcome, note, to_db(started), to_db(ended), duration_seconds,
             to_db(callback_at), to_db(meeting_at), session_id, now_db()),
        )
        call_id = int(cur.lastrowid)
        prior_calls = conn.execute("SELECT COUNT(*) FROM calls WHERE lead_id=? AND id != ?", (lead_id, call_id)).fetchone()[0]
        # Whoever first logs a call on a lead becomes its owner.
        owner_id = lead["owner_id"]
        if user_id is not None and (owner_id is None or prior_calls == 0):
            owner_id = user_id
        no_answers = lead["consecutive_no_answer"] + 1 if outcome == "no_answer" else 0
        new_status = STATUS_AFTER[outcome]
        if lead["status"] in ("proposal_sent", "won") and outcome in ("no_answer", "gatekeeper", "connected"):
            new_status = lead["status"]  # don't move a deal backwards because of a routine call
        stage_changed = new_status != lead["status"]
        conn.execute(
            """UPDATE leads SET status=?, last_called_at=?, last_outcome=?, consecutive_no_answer=?,
                   callback_at=?, owner_id=?, locked_by=NULL, locked_until=NULL,
                   stage_changed_at=CASE WHEN ? THEN ? ELSE stage_changed_at END WHERE id=?""",
            (new_status, now_db(), outcome, no_answers, to_db(callback_at) if outcome == "callback" else None,
             owner_id, 1 if stage_changed else 0, now_db(), lead_id),
        )
        meeting_id = None
        if outcome == "meeting_booked":
            cur = conn.execute(
                "INSERT INTO meetings(lead_id, user_id, call_id, start_at, duration_minutes, notes, created_at) VALUES (?,?,?,?,?,?,?)",
                (lead_id, owner_id, call_id, to_db(meeting_at), meeting_minutes(conn), note, now_db()),
            )
            meeting_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO stage_changes(lead_id, user_id, from_stage, to_stage, created_at) VALUES (?,?,?,?,?)",
                (lead_id, user_id, lead["status"], "meeting_booked", now_db()),
            )
        for t in tags:
            conn.execute(
                "INSERT INTO call_objections(call_id, lead_id, user_id, tag, other_text, created_at) VALUES (?,?,?,?,?,?)",
                (call_id, lead_id, user_id, t, objection_other if t == "other" else None, now_db()),
            )
        if session_id:
            conn.execute(
                """UPDATE dial_sessions SET dials = dials + 1,
                       connects = connects + ?, meetings = meetings + ? WHERE id=? AND user_id IS ?""",
                (1 if outcome in CONNECT_OUTCOMES else 0, 1 if outcome == "meeting_booked" else 0, session_id, user_id),
            )
        summary = OUTCOMES[outcome]
        if note:
            summary += f": {note[:80]}"
        conn.execute(
            "INSERT INTO activity(user_id, lead_id, kind, summary, tag, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, lead_id, "call", summary, OUTCOMES[outcome], now_db()),
        )
    return {"call_id": call_id, "meeting_id": meeting_id, "lead": require_lead(conn, lead_id)}


def log_dnc_outcome(
    conn: sqlite3.Connection, lead_id: int, user_id: int | None, reason: str | None = None,
    do_not_email: bool = False, duration_seconds: int = 0, session_id: int | None = None,
) -> None:
    """The Dial page's 'Do not call' button: log the call, add the number to DNC, close the lead."""
    lead = require_lead(conn, lead_id)
    with db.transaction(conn):
        ended = now_utc()
        conn.execute(
            """INSERT INTO calls(lead_id, user_id, outcome, note, started_at, ended_at, duration_seconds, session_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (lead_id, user_id, "dnc", reason, to_db(ended - timedelta(seconds=duration_seconds)), to_db(ended),
             duration_seconds, session_id, now_db()),
        )
        if session_id:
            conn.execute("UPDATE dial_sessions SET dials = dials + 1 WHERE id=?", (session_id,))
        if lead["phone"]:
            dnc_add(conn, lead["phone"], reason or "Asked not to be called", user_id, lead_id,
                    also_email=lead["email"] if do_not_email and lead["email"] else None)
        conn.execute(
            "UPDATE leads SET status='dnc', last_called_at=?, last_outcome='dnc', locked_by=NULL, locked_until=NULL, "
            "blocked_reason='Asked not to be called', stage_changed_at=? WHERE id=?",
            (now_db(), now_db(), lead_id),
        )


def set_stage(
    conn: sqlite3.Connection,
    lead_id: int,
    user_id: int | None,
    stage: str,
    setup_fee: float | None = None,
    retainer: float | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    if stage not in STAGES:
        raise LeadError(f"Stage must be one of: {', '.join(STAGES)}")
    lead = require_lead(conn, lead_id)
    with db.transaction(conn):
        conn.execute(
            """INSERT INTO stage_changes(lead_id, user_id, from_stage, to_stage, setup_fee, retainer, reason, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (lead_id, user_id, lead["status"], stage, setup_fee, retainer, reason, now_db()),
        )
        fields = {"status": stage, "stage_changed_at": now_db()}
        if stage == "won":
            fields["setup_fee"] = float(setup_fee or 0)
            fields["retainer"] = float(retainer or 0)
            fields["lost_reason"] = None
        if stage == "lost":
            fields["lost_reason"] = reason
        if lead["owner_id"] is None and user_id is not None:
            fields["owner_id"] = user_id
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE leads SET {sets} WHERE id=?", [*fields.values(), lead_id])
        label = {"meeting_booked": "Meeting booked", "proposal_sent": "Proposal sent", "won": "Won", "lost": "Lost"}[stage]
        extra = ""
        if stage == "won":
            extra = f": £{float(setup_fee or 0):,.0f} setup + £{float(retainer or 0):,.0f}/mo"
        elif stage == "lost" and reason:
            extra = f": {reason}"
        conn.execute(
            "INSERT INTO activity(user_id, lead_id, kind, summary, tag, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, lead_id, "stage", label + extra, label, now_db()),
        )
        if stage in ("won", "lost", "proposal_sent"):
            conn.execute(
                "UPDATE meetings SET status='held', updated_at=? WHERE lead_id=? AND status='scheduled' AND start_at <= ?",
                (now_db(), lead_id, now_db()),
            )
    return require_lead(conn, lead_id)


def set_objection_note(conn: sqlite3.Connection, tag: str, rebuttal: str, user_id: int | None) -> None:
    conn.execute(
        "INSERT INTO objection_notes(tag, rebuttal, updated_by, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(tag) DO UPDATE SET rebuttal=excluded.rebuttal, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
        (tag, rebuttal, user_id, now_db()),
    )
    db.commit(conn)


def objection_notes(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["tag"]: r["rebuttal"] for r in conn.execute("SELECT tag, rebuttal FROM objection_notes").fetchall()}


def objection_counts(conn: sqlite3.Connection, since: str | None = None, user_ids: list[int] | None = None) -> list[dict[str, Any]]:
    sql = "SELECT tag, COUNT(*) AS n FROM call_objections WHERE 1=1"
    args: list[Any] = []
    if since:
        sql += " AND created_at >= ?"
        args.append(since)
    if user_ids:
        sql += f" AND user_id IN ({','.join('?' * len(user_ids))})"
        args += user_ids
    sql += " GROUP BY tag ORDER BY n DESC"
    return [{"tag": r["tag"], "label": objection_label(conn, r["tag"]), "n": r["n"]} for r in conn.execute(sql, args).fetchall()]


# ---------------------------------------------------------------------------
# Dial sessions and manual entries
# ---------------------------------------------------------------------------

def start_session(conn: sqlite3.Connection, user_id: int) -> int:
    cur = conn.execute("INSERT INTO dial_sessions(user_id, started_at) VALUES (?,?)", (user_id, now_db()))
    db.commit(conn)
    return int(cur.lastrowid)


def end_session(conn: sqlite3.Connection, session_id: int, user_id: int) -> None:
    conn.execute(
        "UPDATE dial_sessions SET ended_at=? WHERE id=? AND user_id=? AND ended_at IS NULL", (now_db(), session_id, user_id)
    )
    db.commit(conn)


def add_manual_entry(
    conn: sqlite3.Connection, user_id: int, entry_type: str, count: int = 1, minutes: int = 0,
    note: str | None = None, entry_date: str | None = None,
) -> int:
    if entry_type not in MANUAL_TYPES:
        raise LeadError(f"Type must be one of: {', '.join(MANUAL_TYPES)}")
    d = entry_date or today_london().isoformat()
    cur = conn.execute(
        "INSERT INTO manual_entries(user_id, entry_type, count, minutes, note, entry_date, created_at) VALUES (?,?,?,?,?,?,?)",
        (user_id, entry_type, max(1, int(count)), max(0, int(minutes)), note, d, now_db()),
    )
    db.commit(conn)
    conn.execute(
        "INSERT INTO activity(user_id, lead_id, kind, summary, tag, created_at) VALUES (?,?,?,?,?,?)",
        (user_id, None, "manual", f"{entry_type} x{count}" + (f": {note[:60]}" if note else ""), entry_type, now_db()),
    )
    db.commit(conn)
    return int(cur.lastrowid)


def parse_duration(text: str | None) -> int:
    """'95', '1:35' or '1m35s' -> seconds."""
    if not text:
        return 0
    t = str(text).strip().lower()
    try:
        if ":" in t:
            parts = [int(p) for p in t.split(":")]
            secs = 0
            for p in parts:
                secs = secs * 60 + p
            return secs
        if t.endswith("s") or "m" in t:
            mins, _, rest = t.partition("m")
            if not _:
                return int(t.rstrip("s"))
            return int(mins or 0) * 60 + int(rest.rstrip("s") or 0)
        return int(float(t))
    except ValueError:
        raise LeadError(f"Couldn't read duration '{text}'. Use seconds (95) or mm:ss (1:35).") from None


