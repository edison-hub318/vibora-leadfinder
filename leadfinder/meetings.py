"""Meetings: listing, outcomes, .ics files and Google Calendar links."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from . import db
from .calls import set_stage
from .leads import LeadError
from .phones import pretty
from .timeutil import LONDON, UTC, from_db, now_db, now_utc


def list_meetings(conn: sqlite3.Connection, user_ids: list[int] | None = None) -> dict[str, list[dict[str, Any]]]:
    sql = """SELECT m.*, l.name, l.phone, l.address, l.trade, l.town, l.status AS lead_status, u.initials, u.full_name
             FROM meetings m JOIN leads l ON l.id=m.lead_id LEFT JOIN users u ON u.id=m.user_id
             WHERE m.status IN ('scheduled')"""
    args: list[Any] = []
    if user_ids:
        sql += f" AND m.user_id IN ({','.join('?' * len(user_ids))})"
        args += user_ids
    sql += " ORDER BY m.start_at"
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    now = now_db()
    upcoming = [r for r in rows if r["start_at"] >= now]
    awaiting = [r for r in rows if r["start_at"] < now]
    return {"upcoming": upcoming, "awaiting": awaiting}


def get_meeting(conn: sqlite3.Connection, meeting_id: int) -> dict[str, Any]:
    row = conn.execute(
        """SELECT m.*, l.name, l.phone, l.address, l.trade, l.town, l.website, l.maps_url
           FROM meetings m JOIN leads l ON l.id=m.lead_id WHERE m.id=?""",
        (meeting_id,),
    ).fetchone()
    if row is None:
        raise LeadError(f"No meeting with ID {meeting_id}.")
    return dict(row)


def meeting_outcome(conn: sqlite3.Connection, meeting_id: int, outcome: str, user_id: int | None,
                    reason: str | None = None) -> None:
    """Past meeting: 'proposal' (held -> proposal sent), 'lost' (held -> lost) or 'no_show'."""
    m = get_meeting(conn, meeting_id)
    if outcome == "proposal":
        conn.execute("UPDATE meetings SET status='held', updated_at=? WHERE id=?", (now_db(), meeting_id))
        db.commit(conn)
        set_stage(conn, m["lead_id"], user_id, "proposal_sent")
    elif outcome == "lost":
        conn.execute("UPDATE meetings SET status='held', updated_at=? WHERE id=?", (now_db(), meeting_id))
        db.commit(conn)
        set_stage(conn, m["lead_id"], user_id, "lost", reason=reason or "Lost after meeting")
    elif outcome == "no_show":
        with db.transaction(conn):
            conn.execute("UPDATE meetings SET status='no_show', updated_at=? WHERE id=?", (now_db(), meeting_id))
            conn.execute(
                "UPDATE leads SET status='callback', callback_at=?, stage_changed_at=? WHERE id=?",
                (now_db(), now_db(), m["lead_id"]),
            )
            conn.execute(
                "INSERT INTO activity(user_id, lead_id, kind, summary, tag, created_at) VALUES (?,?,?,?,?,?)",
                (user_id, m["lead_id"], "meeting", "Meeting no-show: back to callbacks", "No-show", now_db()),
            )
    else:
        raise LeadError("Outcome must be proposal, lost or no_show.")


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def _ics_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Fold lines longer than 75 octets (RFC 5545)."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts = []
    while raw:
        cut = 75 if not parts else 74
        chunk = raw[:cut]
        while True:  # don't split a UTF-8 character
            try:
                chunk.decode("utf-8")
                break
            except UnicodeDecodeError:
                cut -= 1
                chunk = raw[:cut]
        parts.append(chunk.decode("utf-8"))
        raw = raw[cut:]
    return "\r\n ".join(parts)


VTIMEZONE_LONDON = [
    "BEGIN:VTIMEZONE",
    "TZID:Europe/London",
    "X-LIC-LOCATION:Europe/London",
    "BEGIN:DAYLIGHT",
    "TZOFFSETFROM:+0000",
    "TZOFFSETTO:+0100",
    "TZNAME:BST",
    "DTSTART:19700329T010000",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
    "END:DAYLIGHT",
    "BEGIN:STANDARD",
    "TZOFFSETFROM:+0100",
    "TZOFFSETTO:+0000",
    "TZNAME:GMT",
    "DTSTART:19701025T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
    "END:STANDARD",
    "END:VTIMEZONE",
]


def _description(m: dict[str, Any]) -> str:
    parts = [f"{m['name']}"]
    if m.get("phone"):
        parts.append(f"Phone: {pretty(m['phone'])}")
    if m.get("address"):
        parts.append(f"Address: {m['address']}")
    if m.get("notes"):
        parts.append(f"Call notes: {m['notes']}")
    return "\n".join(parts)


def meeting_times(m: dict[str, Any]) -> tuple[datetime, datetime]:
    start = from_db(m["start_at"])
    assert start is not None
    return start, start + timedelta(minutes=int(m.get("duration_minutes") or 30))


def build_ics(m: dict[str, Any]) -> str:
    start, end = meeting_times(m)
    ls, le = start.astimezone(LONDON), end.astimezone(LONDON)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Vibora//Leadfinder//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        *VTIMEZONE_LONDON,
        "BEGIN:VEVENT",
        f"UID:meeting-{m['id']}@leadfinder.vibora",
        f"DTSTAMP:{now_utc().astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;TZID=Europe/London:{ls.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND;TZID=Europe/London:{le.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{_ics_escape('Meeting: ' + m['name'])}",
        f"DESCRIPTION:{_ics_escape(_description(m))}",
        f"LOCATION:{_ics_escape(m.get('address') or '')}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def google_calendar_url(m: dict[str, Any]) -> str:
    start, end = meeting_times(m)
    fmt = "%Y%m%dT%H%M%SZ"
    params = {
        "action": "TEMPLATE",
        "text": f"Meeting: {m['name']}",
        "dates": f"{start.astimezone(UTC).strftime(fmt)}/{end.astimezone(UTC).strftime(fmt)}",
        "details": _description(m),
        "location": m.get("address") or "",
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(params, quote_via=quote)
