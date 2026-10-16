"""Lead queries shared by the CLI and the dashboard: listing, detail, the `next` queue,
soft locks, ownership, TPS, DNC and CSV export."""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import db, phones
from .timeutil import london_day_bounds, now_db, now_utc, to_db, today_london
from .users import get_user, log_activity

LOCK_MINUTES = 10
CLOSED = ("won", "lost", "not_interested", "wrong_number", "dnc", "duplicate")
STATUS_FILTERS = ("new", "called", "callback", "interested", "not_interested", "client", "meeting_booked",
                  "proposal_sent", "won", "lost", "wrong_number", "dnc")
DNC_OR_CLOSED_FOR_CALLING = ("won", "lost", "not_interested", "wrong_number", "dnc", "duplicate")


class LeadError(Exception):
    pass


def get_lead(conn: sqlite3.Connection, lead_id: int) -> dict[str, Any] | None:
    return db.row_to_dict(conn.execute(
        """SELECT l.*, o.full_name AS owner_name, o.initials AS owner_initials
           FROM leads l LEFT JOIN users o ON o.id = l.owner_id WHERE l.id=?""",
        (lead_id,),
    ).fetchone())


def require_lead(conn: sqlite3.Connection, lead_id: int) -> dict[str, Any]:
    lead = get_lead(conn, lead_id)
    if lead is None:
        raise LeadError(f"No lead with ID {lead_id}.")
    return lead


def is_dnc(conn: sqlite3.Connection, phone: str | None) -> bool:
    e = phones.normalise_e164(phone)
    if not e:
        return False
    return conn.execute("SELECT 1 FROM dnc WHERE phone_e164=?", (e,)).fetchone() is not None


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def list_leads(
    conn: sqlite3.Connection,
    town: str | None = None,
    trade: str | None = None,
    status: str | None = None,
    no_website: bool = False,
    website_status: str | None = None,
    owner_id: int | None = None,
    q: str | None = None,
    sort: str = "score",
    top: int | None = 25,
    for_calling: bool = False,
) -> list[dict[str, Any]]:
    sql = """SELECT l.*, o.initials AS owner_initials, o.full_name AS owner_name FROM leads l
             LEFT JOIN users o ON o.id = l.owner_id WHERE l.duplicate_of IS NULL"""
    args: list[Any] = []
    if town:
        sql += " AND lower(l.town) = lower(?)"
        args.append(town)
    if trade:
        sql += " AND (lower(l.trade) = lower(?) OR lower(replace(l.trade,'_',' ')) = lower(?))"
        args += [trade, trade]
    if status:
        st = "won" if status == "client" else status
        sql += " AND l.status = ?"
        args.append(st)
    else:
        sql += " AND l.status != 'duplicate'"
    if no_website:
        sql += " AND (l.website_status IN ('none','social_only') OR l.website IS NULL OR l.website = '')"
    if website_status:
        sql += " AND l.website_status = ?"
        args.append(website_status)
    if owner_id:
        sql += " AND l.owner_id = ?"
        args.append(owner_id)
    if q:
        sql += " AND (l.name LIKE ? OR l.phone LIKE ? OR l.address LIKE ?)"
        args += [f"%{q}%"] * 3
    if for_calling:
        sql += f" AND l.status NOT IN ({','.join('?' * len(DNC_OR_CLOSED_FOR_CALLING))})"
        args += list(DNC_OR_CLOSED_FOR_CALLING)
        sql += " AND l.tps_status != 'registered' AND (l.phone_e164 IS NULL OR l.phone_e164 NOT IN (SELECT phone_e164 FROM dnc))"
    order = {
        "score": "l.score DESC, l.id",
        "name": "l.name COLLATE NOCASE",
        "town": "l.town, l.score DESC",
        "trade": "l.trade, l.score DESC",
        "status": "l.status, l.score DESC",
        "website": "l.website_status, l.score DESC",
        "reviews": "l.review_count DESC",
        "recent": "l.id DESC",
    }.get(sort, "l.score DESC, l.id")
    sql += f" ORDER BY {order}"
    if top:
        sql += " LIMIT ?"
        args.append(top)
    return [db.row_to_dict(r) for r in conn.execute(sql, args).fetchall()]  # type: ignore[misc]


def distinct_values(conn: sqlite3.Connection, column: str) -> list[str]:
    assert column in ("town", "trade")
    return [r[0] for r in conn.execute(
        f"SELECT DISTINCT {column} FROM leads WHERE {column} IS NOT NULL AND duplicate_of IS NULL ORDER BY {column}"
    ).fetchall()]


def call_history(conn: sqlite3.Connection, lead_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT c.*, u.initials, u.full_name,
                  (SELECT group_concat(tag, ', ') FROM call_objections o WHERE o.call_id = c.id) AS objections
           FROM calls c LEFT JOIN users u ON u.id = c.user_id WHERE c.lead_id=? ORDER BY c.id DESC""",
        (lead_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def lead_timeline(conn: sqlite3.Connection, lead_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT a.*, u.initials FROM activity a LEFT JOIN users u ON u.id=a.user_id
           WHERE a.lead_id=? ORDER BY a.id DESC LIMIT 50""",
        (lead_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Soft locks and ownership
# ---------------------------------------------------------------------------

def lock_lead(conn: sqlite3.Connection, lead_id: int, user_id: int) -> str | None:
    """Soft-lock a lead to this user for 10 minutes. Returns a warning if someone else holds it."""
    lead = require_lead(conn, lead_id)
    if lead["locked_by"] and lead["locked_by"] != user_id and lead["locked_until"] and lead["locked_until"] > now_db():
        other = get_user(conn, lead["locked_by"])
        return f"{other.first_name if other else 'Your partner'} is on this one"
    conn.execute(
        "UPDATE leads SET locked_by=?, locked_until=? WHERE id=?",
        (user_id, to_db(now_utc() + timedelta(minutes=LOCK_MINUTES)), lead_id),
    )
    db.commit(conn)
    return None


def unlock_lead(conn: sqlite3.Connection, lead_id: int, user_id: int) -> None:
    conn.execute("UPDATE leads SET locked_by=NULL, locked_until=NULL WHERE id=? AND locked_by=?", (lead_id, user_id))
    db.commit(conn)


def assign_owner(conn: sqlite3.Connection, lead_id: int, owner_id: int | None, by_user: int | None) -> None:
    require_lead(conn, lead_id)
    conn.execute("UPDATE leads SET owner_id=? WHERE id=?", (owner_id, lead_id))
    db.commit(conn)
    owner = get_user(conn, owner_id)
    log_activity(conn, by_user, "assign", f"Assigned to {owner.full_name if owner else 'nobody'}", lead_id)


# ---------------------------------------------------------------------------
# The calling queue
# ---------------------------------------------------------------------------

def _queue_sql(user_id: int | None) -> tuple[str, list[Any]]:
    now = now_db()
    _, end_today = london_day_bounds(today_london())
    stale = to_db(now_utc() - timedelta(days=30))
    retry_after = to_db(now_utc() - timedelta(hours=4))
    me = user_id if user_id is not None else -1
    sql = f"""
    SELECT l.*,
      CASE
        WHEN l.status='callback' AND l.callback_at IS NOT NULL AND l.callback_at < :end_today
             AND (l.owner_id = :me OR l.owner_id IS NULL) THEN 0
        WHEN l.status='new' AND (l.owner_id = :me OR l.owner_id IS NULL) THEN 1
        WHEN l.status IN ('called','interested') AND l.consecutive_no_answer < 3
             AND (l.owner_id = :me OR l.owner_id IS NULL) AND COALESCE(l.last_called_at,'') < :retry_after THEN 2
        WHEN l.status='new' AND COALESCE(
             (SELECT MAX(c.created_at) FROM calls c WHERE c.lead_id=l.id), l.first_seen) < :stale THEN 3
        WHEN l.status IN ('called','interested') AND l.consecutive_no_answer >= 3
             AND (l.owner_id = :me OR l.owner_id IS NULL) AND COALESCE(l.last_called_at,'') < :retry_after THEN 4
        ELSE NULL
      END AS queue_rank
    FROM leads l
    WHERE l.duplicate_of IS NULL
      AND l.status NOT IN ({",".join("'" + s + "'" for s in CLOSED)})
      AND l.tps_status != 'registered'
      AND l.blocked_reason IS NULL
      AND l.phone_e164 IS NOT NULL AND l.phone_e164 != ''
      AND l.phone_e164 NOT IN (SELECT phone_e164 FROM dnc)
      AND NOT (l.locked_by IS NOT NULL AND l.locked_by != :me AND COALESCE(l.locked_until,'') > :now)
    """
    return sql, {"end_today": end_today, "me": me, "stale": stale, "retry_after": retry_after, "now": now}  # type: ignore[return-value]


def queue(conn: sqlite3.Connection, user_id: int | None, limit: int = 10) -> list[dict[str, Any]]:
    """Leads in calling order: my callbacks due today, then the best new leads, then retries,
    then (last) leads with three no-answers in a row."""
    sql, args = _queue_sql(user_id)
    sql = f"SELECT * FROM ({sql}) WHERE queue_rank IS NOT NULL ORDER BY queue_rank, " \
          f"CASE WHEN queue_rank=0 THEN callback_at END, score DESC, id LIMIT :limit"
    args["limit"] = limit  # type: ignore[index]
    return [db.row_to_dict(r) for r in conn.execute(sql, args).fetchall()]  # type: ignore[misc]


def next_lead(conn: sqlite3.Connection, user_id: int | None, lock: bool = True) -> dict[str, Any] | None:
    rows = queue(conn, user_id, limit=1)
    if not rows:
        return None
    lead = rows[0]
    if lock and user_id is not None:
        lock_lead(conn, lead["id"], user_id)
    return get_lead(conn, lead["id"])


def tps_warning(lead: dict[str, Any]) -> str | None:
    if lead.get("tps_status") == "unchecked":
        return "TPS not checked: check this number against the TPS/CTPS list before calling."
    return None


def call_opener(lead: dict[str, Any], my_name: str) -> str:
    me = my_name or "<your name>"
    trade = (lead.get("trade") or "business").replace("_", " ")
    return (
        f"Hi, is that the owner of {lead['name']}? It's {me} from Vibora, a small web design studio. "
        f"I was looking at {trade} in {lead.get('town') or 'the area'} on Google and noticed something "
        f"I thought you'd want to know. Have you got two minutes?"
    )


def display_hook(lead: dict[str, Any]) -> str:
    return lead.get("ai_verdict") or lead.get("hook") or ""


# ---------------------------------------------------------------------------
# TPS
# ---------------------------------------------------------------------------

def set_tps(conn: sqlite3.Connection, lead_id: int, registered: bool, user_id: int | None) -> None:
    require_lead(conn, lead_id)
    conn.execute(
        "UPDATE leads SET tps_status=?, tps_checked_at=?, tps_checked_by=? WHERE id=?",
        ("registered" if registered else "clear", now_db(), user_id, lead_id),
    )
    db.commit(conn)
    log_activity(conn, user_id, "tps", "TPS: registered (do not call)" if registered else "TPS: clear", lead_id,
                 "TPS")


# ---------------------------------------------------------------------------
# Do Not Call
# ---------------------------------------------------------------------------

def dnc_add(
    conn: sqlite3.Connection, phone: str, reason: str | None, user_id: int | None,
    lead_id: int | None = None, also_email: str | None = None,
) -> str:
    e164 = phones.normalise_e164(phone)
    if not e164:
        raise LeadError(f"'{phone}' doesn't look like a valid phone number.")
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO dnc(phone_e164, phone_raw, reason, lead_id, user_id, created_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(phone_e164) DO UPDATE SET reason=COALESCE(excluded.reason, dnc.reason)",
            (e164, phone, reason, lead_id, user_id, now_db()),
        )
        affected = conn.execute("SELECT id FROM leads WHERE phone_e164=?", (e164,)).fetchall()
        conn.execute(
            "UPDATE leads SET status='dnc', blocked_reason='Asked not to be called', stage_changed_at=? WHERE phone_e164=?",
            (now_db(), e164),
        )
        for r in affected:
            conn.execute(
                "INSERT INTO activity(user_id, lead_id, kind, summary, tag, created_at) VALUES (?,?,?,?,?,?)",
                (user_id, r["id"], "dnc", f"Added to Do Not Call{': ' + reason if reason else ''}", "DNC", now_db()),
            )
        if also_email:
            conn.execute(
                "INSERT OR IGNORE INTO suppression(email, reason, added_by, created_at) VALUES (?,?,?,?)",
                (also_email.strip().lower(), "Do not email (DNC)", user_id, now_db()),
            )
    return e164


def dnc_remove(conn: sqlite3.Connection, phone: str, user_id: int | None) -> bool:
    e164 = phones.normalise_e164(phone)
    cur = conn.execute("DELETE FROM dnc WHERE phone_e164=?", (e164,))
    # Leads stay closed; reopen them by hand if wanted.
    db.commit(conn)
    if cur.rowcount:
        log_activity(conn, user_id, "dnc_remove", f"Removed {e164} from Do Not Call")
    return cur.rowcount > 0


def dnc_list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        """SELECT d.*, u.initials, l.name AS lead_name FROM dnc d
           LEFT JOIN users u ON u.id=d.user_id LEFT JOIN leads l ON l.id=d.lead_id ORDER BY d.id DESC"""
    ).fetchall()]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

EXPORT_COLUMNS = [
    "id", "name", "trade", "town", "phone", "phone_type", "email", "website", "website_status", "hook",
    "rating", "review_count", "score", "status", "entity_type", "tps_status", "owner_name", "callback_at",
    "address", "postcode", "maps_url",
]


def export_csv(conn: sqlite3.Connection, path: Path, for_calling: bool = True, **filters: Any) -> int:
    """Write leads to CSV (UTF-8 with BOM so Excel opens it correctly). DNC and TPS-registered
    numbers are always left out of exports meant for calling."""
    rows = list_leads(conn, top=None, for_calling=for_calling, **filters)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(EXPORT_COLUMNS)
        for r in rows:
            vals = []
            for c in EXPORT_COLUMNS:
                v = r.get(c)
                if isinstance(v, (list, dict)):
                    v = json.dumps(v)
                vals.append("" if v is None else v)
            w.writerow(vals)
    return len(rows)
