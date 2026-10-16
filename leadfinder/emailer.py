"""Cold email: drafting from a template, review, and sending under UK PECR rules.

Hard rules (enforced in `check_send_rules`, used by both the CLI and the dashboard):
  * only limited companies (sole traders/partnerships need prior consent under PECR)
  * never more than the daily send limit, counted from the database
  * never to a suppressed address
  * never a second email to the same lead except one follow-up drafted with `followup`
  * plain text, one recipient, List-Unsubscribe header
"""
from __future__ import annotations

import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any, Callable

from . import db
from .audit import EMAIL_RE
from .clients import Clients, ExternalError
from .config import Settings
from .leads import require_lead
from .timeutil import london_day_bounds, now_db, now_utc, to_db, today_london
from .users import log_activity

MAX_WORDS = 120
STOP_LINE = "Reply STOP or 'unsubscribe' and I won't contact you again."
FOLLOWUP_DAYS = 5
NO_EMAIL_STATUSES = ("won", "lost", "not_interested", "dnc", "wrong_number", "duplicate")


class EmailError(Exception):
    pass


# ---------------------------------------------------------------------------
# Suppression and addresses
# ---------------------------------------------------------------------------

def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.fullmatch((email or "").strip()))


def is_suppressed(conn: sqlite3.Connection, email: str | None) -> bool:
    if not email:
        return False
    return conn.execute("SELECT 1 FROM suppression WHERE email=?", (email.strip().lower(),)).fetchone() is not None


def suppress(conn: sqlite3.Connection, email: str, user_id: int | None, reason: str = "Opted out") -> bool:
    e = email.strip().lower()
    if not valid_email(e):
        raise EmailError(f"'{email}' isn't a valid email address.")
    cur = conn.execute(
        "INSERT OR IGNORE INTO suppression(email, reason, added_by, created_at) VALUES (?,?,?,?)",
        (e, reason, user_id, now_db()),
    )
    # Any unsent drafts to this address are cancelled.
    conn.execute(
        "UPDATE emails SET status='blocked', error='Address suppressed' WHERE lower(to_email)=? AND status IN ('pending_review','approved')",
        (e,),
    )
    db.commit(conn)
    if cur.rowcount:
        log_activity(conn, user_id, "suppress", f"Suppressed {e}")
    return cur.rowcount > 0


def unsuppress(conn: sqlite3.Connection, email: str) -> None:
    conn.execute("DELETE FROM suppression WHERE email=?", (email.strip().lower(),))
    db.commit(conn)


def suppression_list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT s.*, u.initials FROM suppression s LEFT JOIN users u ON u.id=s.added_by ORDER BY s.created_at DESC"
    ).fetchall()]


def extract_addresses(text: str) -> list[str]:
    out: list[str] = []
    for e in EMAIL_RE.findall(text or ""):
        e = e.lower().strip(".")
        if e not in out:
            out.append(e)
    return out


def add_email(conn: sqlite3.Connection, lead_id: int, email: str, user_id: int | None) -> None:
    """An email address is only ever added by hand (here) or by find-emails. Never guessed."""
    if not valid_email(email):
        raise EmailError(f"'{email}' isn't a valid email address.")
    require_lead(conn, lead_id)
    conn.execute("UPDATE leads SET email=?, email_source='manual' WHERE id=?", (email.strip().lower(), lead_id))
    db.commit(conn)
    log_activity(conn, user_id, "email_added", f"Email added: {email.strip().lower()}", lead_id)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

@dataclass
class Rendered:
    subject: str
    body: str  # including footer
    words: int  # words before the footer


def footer(settings: Settings) -> str:
    lines = [settings.my_name or settings.from_name or "Vibora"]
    if settings.my_phone:
        lines.append(settings.my_phone)
    lines.append(settings.business_address or "(set BUSINESS_ADDRESS in .env)")
    lines.append("")
    lines.append(STOP_LINE)
    return "\n".join(lines)


def load_template(settings: Settings, kind: str = "initial") -> str:
    name = "cold_email.txt" if kind == "initial" else "followup_email.txt"
    return settings.template_file(name).read_text(encoding="utf-8")


def render(template: str, lead: dict[str, Any], settings: Settings) -> Rendered:
    """Fill {name} {trade} {town} {hook} {preview_url} {my_name} {my_phone}.
    Lines starting with # are comments. Lines using {preview_url} are dropped when there's no preview."""
    trade = (lead.get("trade") or "business").replace("_", " ")
    values = {
        "name": lead.get("name") or "",
        "trade": trade,
        "town": lead.get("town") or "",
        "hook": (lead.get("hook") or "").rstrip("."),
        "preview_url": lead.get("preview_url") or "",
        "my_name": settings.my_name or "",
        "my_phone": settings.my_phone or "",
    }
    lines = []
    for line in template.replace("\r\n", "\n").split("\n"):
        if line.lstrip().startswith("#"):
            continue
        if "{preview_url}" in line and not values["preview_url"]:
            continue
        lines.append(line)
    text = "\n".join(lines).strip("\n")
    subject = "Quick question"
    if text.lower().startswith("subject:"):
        first, _, rest = text.partition("\n")
        subject = first.split(":", 1)[1].strip()
        text = rest.strip("\n")
    try:
        subject = subject.format(**values)
        body = text.format(**values)
    except (KeyError, IndexError, ValueError) as e:
        raise EmailError(f"Template has an unknown placeholder: {e}. Use {{name}}, {{trade}}, {{town}}, {{hook}}, {{preview_url}}.") from None
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    words = len(body.split())
    return Rendered(subject=subject, body=body + "\n\n--\n" + footer(settings), words=words)


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------

def _create_draft(conn: sqlite3.Connection, lead: dict[str, Any], r: Rendered, kind: str, user_id: int | None) -> int:
    cur = conn.execute(
        "INSERT INTO emails(lead_id, kind, to_email, subject, body, status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (lead["id"], kind, lead["email"], r.subject, r.body, "pending_review", user_id, now_db()),
    )
    db.commit(conn)
    log_activity(conn, user_id, "email_draft", f"Drafted {'follow-up' if kind == 'followup' else 'cold'} email", lead["id"], "Draft")
    return int(cur.lastrowid)


@dataclass
class DraftReport:
    created: list[int] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (lead name, reason)


def draft(conn: sqlite3.Connection, settings: Settings, user_id: int | None, limit: int = 20) -> DraftReport:
    """Draft first emails for leads with an email, nothing sent before, and not suppressed."""
    rep = DraftReport()
    template = load_template(settings, "initial")
    rows = conn.execute(
        f"""SELECT * FROM leads l WHERE duplicate_of IS NULL AND email IS NOT NULL AND email != ''
            AND status NOT IN ({','.join('?' * len(NO_EMAIL_STATUSES))})
            AND NOT EXISTS (SELECT 1 FROM emails e WHERE e.lead_id=l.id AND e.status NOT IN ('deleted'))
            ORDER BY score DESC LIMIT ?""",
        (*NO_EMAIL_STATUSES, limit * 3),
    ).fetchall()
    for row in rows:
        if len(rep.created) >= limit:
            break
        lead = db.row_to_dict(row)
        assert lead is not None
        if is_suppressed(conn, lead["email"]):
            rep.skipped.append((lead["name"], "address is on the suppression list"))
            continue
        r = render(template, lead, settings)
        if r.words > MAX_WORDS:
            rep.skipped.append((lead["name"], f"draft is {r.words} words; the limit is {MAX_WORDS}. Shorten the template."))
            continue
        rep.created.append(_create_draft(conn, lead, r, "initial", user_id))
    return rep


def followup(conn: sqlite3.Connection, settings: Settings, user_id: int | None, limit: int = 20) -> DraftReport:
    """Draft ONE follow-up for leads whose first email went 5+ days ago with no reply."""
    rep = DraftReport()
    template = load_template(settings, "followup")
    cutoff = to_db(now_utc() - timedelta(days=FOLLOWUP_DAYS))
    rows = conn.execute(
        f"""SELECT l.* FROM leads l JOIN emails e ON e.lead_id=l.id AND e.kind='initial' AND e.status='sent'
            WHERE e.sent_at <= ? AND l.email_replied = 0
            AND l.status NOT IN ({','.join('?' * len(NO_EMAIL_STATUSES))})
            AND NOT EXISTS (SELECT 1 FROM emails f WHERE f.lead_id=l.id AND f.kind='followup' AND f.status != 'deleted')
            ORDER BY e.sent_at LIMIT ?""",
        (cutoff, *NO_EMAIL_STATUSES, limit),
    ).fetchall()
    for row in rows:
        lead = db.row_to_dict(row)
        assert lead is not None
        if is_suppressed(conn, lead["email"]):
            rep.skipped.append((lead["name"], "address is on the suppression list"))
            continue
        r = render(template, lead, settings)
        if r.words > MAX_WORDS:
            rep.skipped.append((lead["name"], f"follow-up is {r.words} words; the limit is {MAX_WORDS}."))
            continue
        rep.created.append(_create_draft(conn, lead, r, "followup", user_id))
    return rep


def mark_replied(conn: sqlite3.Connection, lead_id: int, user_id: int | None) -> None:
    require_lead(conn, lead_id)
    conn.execute("UPDATE leads SET email_replied=1 WHERE id=?", (lead_id,))
    conn.execute("UPDATE emails SET status='skipped' WHERE lead_id=? AND kind='followup' AND status IN ('pending_review','approved')", (lead_id,))
    db.commit(conn)
    log_activity(conn, user_id, "email_reply", "Replied to our email", lead_id, "Replied")


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

def drafts(conn: sqlite3.Connection, status: str = "pending_review") -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT e.*, l.name, l.entity_type, l.trade, l.town, u.initials AS created_initials,
                  a.initials AS approved_initials, s.initials AS sent_initials
           FROM emails e JOIN leads l ON l.id=e.lead_id
           LEFT JOIN users u ON u.id=e.created_by LEFT JOIN users a ON a.id=e.approved_by
           LEFT JOIN users s ON s.id=e.sent_by
           WHERE e.status=? ORDER BY e.id""",
        (status,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["block_reason"] = block_reason_for(conn, d)
        out.append(d)
    return out


def get_email(conn: sqlite3.Connection, email_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT e.*, l.name, l.entity_type FROM emails e JOIN leads l ON l.id=e.lead_id WHERE e.id=?", (email_id,)).fetchone()
    if row is None:
        raise EmailError(f"No email with ID {email_id}.")
    return dict(row)


def approve(conn: sqlite3.Connection, email_id: int, user_id: int | None) -> None:
    e = get_email(conn, email_id)
    if e["status"] != "pending_review":
        raise EmailError("Only drafts awaiting review can be approved.")
    conn.execute("UPDATE emails SET status='approved', approved_by=?, approved_at=? WHERE id=?", (user_id, now_db(), email_id))
    db.commit(conn)
    log_activity(conn, user_id, "email_approve", "Approved email draft", e["lead_id"], "Approved")


def edit(conn: sqlite3.Connection, email_id: int, subject: str, body: str, user_id: int | None) -> None:
    e = get_email(conn, email_id)
    if e["status"] not in ("pending_review", "approved"):
        raise EmailError("Sent emails can't be edited.")
    if STOP_LINE not in body:
        raise EmailError("The footer with the opt-out line must stay in the email.")
    words = len(body.split("\n--\n")[0].split())
    if words > MAX_WORDS:
        raise EmailError(f"The email is {words} words; keep it under {MAX_WORDS}.")
    conn.execute("UPDATE emails SET subject=?, body=?, status='pending_review' WHERE id=?", (subject.strip(), body, email_id))
    db.commit(conn)
    log_activity(conn, user_id, "email_edit", "Edited email draft", e["lead_id"])


def delete(conn: sqlite3.Connection, email_id: int, user_id: int | None) -> None:
    e = get_email(conn, email_id)
    if e["status"] == "sent":
        raise EmailError("Sent emails can't be deleted.")
    conn.execute("UPDATE emails SET status='deleted' WHERE id=?", (email_id,))
    db.commit(conn)
    log_activity(conn, user_id, "email_delete", "Deleted email draft", e["lead_id"])


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def daily_limit(conn: sqlite3.Connection, settings: Settings) -> int:
    v = db.get_setting(conn, "daily_send_limit")
    try:
        return int(v) if v not in (None, "") else settings.daily_send_limit
    except (TypeError, ValueError):
        return settings.daily_send_limit


def sent_today(conn: sqlite3.Connection) -> int:
    start, end = london_day_bounds(today_london())
    return int(conn.execute(
        "SELECT COUNT(*) FROM emails WHERE status='sent' AND sent_at >= ? AND sent_at < ?", (start, end)
    ).fetchone()[0])


def block_reason_for(conn: sqlite3.Connection, e: dict[str, Any]) -> str | None:
    """Why this email may not be sent (None if it may). Shown in the UI and the CLI."""
    lead = require_lead(conn, e["lead_id"])
    et = lead["entity_type"]
    if et != "limited_company":
        if et in ("sole_trader", "likely_sole_trader"):
            return "Sole trader: email not allowed without consent, call instead"
        return "Company type unknown: confirm it's a limited company first (classify or set-type)"
    if is_suppressed(conn, e["to_email"]) or is_suppressed(conn, lead["email"]):
        return "Address is on the suppression list"
    if lead["status"] in ("dnc",):
        return "Lead is on the Do Not Call list"
    sent_same_kind = conn.execute(
        "SELECT COUNT(*) FROM emails WHERE lead_id=? AND kind=? AND status='sent' AND id != ?",
        (e["lead_id"], e["kind"], e["id"]),
    ).fetchone()[0]
    if sent_same_kind:
        return "Already emailed this lead" if e["kind"] == "initial" else "Follow-up already sent"
    if e["kind"] == "followup":
        first = conn.execute("SELECT 1 FROM emails WHERE lead_id=? AND kind='initial' AND status='sent'", (e["lead_id"],)).fetchone()
        if not first:
            return "Follow-up without a first email"
        if lead["email_replied"]:
            return "They replied; no follow-up needed"
    if len(e["body"].split("\n--\n")[0].split()) > MAX_WORDS:
        return f"Longer than {MAX_WORDS} words"
    if STOP_LINE not in e["body"]:
        return "Missing the opt-out footer"
    return None


def build_message(e: dict[str, Any], settings: Settings) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((settings.from_name or "", settings.from_email))
    msg["To"] = e["to_email"]  # exactly one recipient; never BCC
    msg["Subject"] = e["subject"]
    msg["List-Unsubscribe"] = f"<mailto:{settings.from_email}?subject=unsubscribe>"
    msg["Message-ID"] = make_msgid(domain=(settings.from_email.split("@")[-1] or "localhost"))
    msg.set_content(e["body"], subtype="plain", charset="utf-8")
    return msg


@dataclass
class SendReport:
    sent: list[dict[str, Any]] = field(default_factory=list)
    would_send: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[tuple[dict[str, Any], str]] = field(default_factory=list)
    stopped_at_limit: bool = False
    errors: list[tuple[dict[str, Any], str]] = field(default_factory=list)
    limit: int = 0
    sent_before: int = 0

    def skip_summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _, reason in self.skipped:
            out[reason] = out.get(reason, 0) + 1
        return out


def send(
    conn: sqlite3.Connection,
    settings: Settings,
    clients: Clients,
    user_id: int | None,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[float, float], float] = random.uniform,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> SendReport:
    emit = on_event or (lambda *_: None)
    rep = SendReport(limit=daily_limit(conn, settings), sent_before=sent_today(conn))
    approved = conn.execute("SELECT * FROM emails WHERE status='approved' ORDER BY id").fetchall()
    count_today = rep.sent_before
    first = True
    for row in approved:
        e = dict(row)
        reason = block_reason_for(conn, e)
        if reason:
            rep.skipped.append((e, reason))
            continue
        if count_today >= rep.limit:
            rep.stopped_at_limit = True
            break
        if dry_run:
            rep.would_send.append({**e, "message": build_message(e, settings).as_string()})
            count_today += 1
            continue
        if clients.mailer is None:
            raise EmailError("No mail sender configured.")
        if not first:
            delay = rand(60, 180)
            emit("wait", {"seconds": delay})
            sleep(delay)
        first = False
        try:
            clients.mailer.send(build_message(e, settings))
        except ExternalError as err:
            conn.execute("UPDATE emails SET error=? WHERE id=?", (str(err), e["id"]))
            db.commit(conn)
            rep.errors.append((e, str(err)))
            continue
        conn.execute("UPDATE emails SET status='sent', sent_at=?, sent_by=?, error=NULL WHERE id=?", (now_db(), user_id, e["id"]))
        db.commit(conn)
        log_activity(conn, user_id, "email_sent", f"Email sent to {e['to_email']}", e["lead_id"], "Email sent")
        count_today += 1
        rep.sent.append(e)
        emit("sent", {"email": e})
    return rep


def sent_emails(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        """SELECT e.*, l.name, u.initials AS sent_initials FROM emails e JOIN leads l ON l.id=e.lead_id
           LEFT JOIN users u ON u.id=e.sent_by WHERE e.status='sent' ORDER BY e.sent_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()]

