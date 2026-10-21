"""Cold email campaigns: one template sent to a whole segment of leads in batches.

A campaign is a saved filter (country, trade, town, website status, minimum score) plus a
template. Creating it drafts one email per matching lead; approving it approves every draft
that passes the send rules in one go (instead of one by one); sending works through them
within the shared daily limit, so a big campaign simply runs over several days.

Every rule in `emailer` still applies to campaign email: country rules (PECR / CAN-SPAM),
suppression, one first email + one follow-up per lead, the opt-out footer and the daily limit.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import db, emailer
from .config import Settings
from .emailer import NO_EMAIL_STATUSES, EmailError
from .search import norm_country, SearchError
from .timeutil import now_db
from .users import log_activity

# Leads worth emailing about their website. "ok"/"good" sites are a weak pitch, so they're opt-in.
DEFAULT_WEBSITE_STATUSES = ["none", "social_only", "free_builder", "poor"]
ALL_WEBSITE_STATUSES = ["none", "social_only", "free_builder", "poor", "ok", "good", "unchecked"]

# 2026 cold email benchmarks, per 100 emails delivered (Mailshake/Instantly 2026 benchmark tables).
# Lower bound of each band. Edit these if you find better numbers for your market.
REPLY_BANDS = [(0.0, "below average"), (1.5, "average"), (3.0, "good"), (5.0, "great")]
MEETING_BANDS = [(0.0, "below average"), (0.3, "average"), (1.0, "good"), (2.0, "great")]


class CampaignError(Exception):
    pass


@dataclass
class Filters:
    country: str = "GB"
    trade: str | None = None
    town: str | None = None
    website: list[str] = field(default_factory=lambda: list(DEFAULT_WEBSITE_STATUSES))
    min_score: int = 0

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, s: str) -> "Filters":
        d = json.loads(s or "{}")
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def describe(self) -> str:
        parts = ["USA" if self.country == "US" else "UK"]
        if self.trade:
            parts.append(self.trade.replace("_", " "))
        if self.town:
            parts.append(f"in {self.town}")
        parts.append("websites: " + ", ".join(self.website))
        if self.min_score:
            parts.append(f"score {self.min_score}+")
        return " / ".join(parts)


def parse_website(value: str | None) -> list[str]:
    if not value:
        return list(DEFAULT_WEBSITE_STATUSES)
    out = [v.strip().lower() for v in value.split(",") if v.strip()]
    bad = [v for v in out if v not in ALL_WEBSITE_STATUSES]
    if bad:
        raise CampaignError(f"Unknown website status {', '.join(bad)}. Use: {', '.join(ALL_WEBSITE_STATUSES)}.")
    return out


def make_filters(country: str = "GB", trade: str | None = None, town: str | None = None,
                 website: str | list[str] | None = None, min_score: int = 0) -> Filters:
    try:
        c = norm_country(country)
    except SearchError as e:
        raise CampaignError(str(e)) from None
    web = website if isinstance(website, list) else parse_website(website)
    return Filters(country=c, trade=(trade or None), town=(town or None), website=web, min_score=int(min_score or 0))


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def get(conn: sqlite3.Connection, name_or_id: str | int) -> dict[str, Any]:
    key = str(name_or_id).strip()
    row = None
    if key.isdigit():
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (int(key),)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM campaigns WHERE name=? COLLATE NOCASE", (key,)).fetchone()
    if row is None:
        raise CampaignError(f"No campaign called '{name_or_id}'. See `leadfinder campaign list`.")
    d = dict(row)
    d["filters"] = Filters.from_json(d["filters_json"])
    return d


def list_all(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall():
        d = dict(r)
        d["filters"] = Filters.from_json(d["filters_json"])
        d["stats"] = stats(conn, d["id"])
        out.append(d)
    return out


def _segment_sql(f: Filters) -> tuple[str, list[Any]]:
    """WHERE clause for leads in the segment (ignoring email availability)."""
    where = ["l.duplicate_of IS NULL", "COALESCE(l.country,'GB') = ?",
             f"l.status NOT IN ({','.join('?' * len(NO_EMAIL_STATUSES))})", "l.score >= ?"]
    args: list[Any] = [f.country, *NO_EMAIL_STATUSES, f.min_score]
    if f.trade:
        where.append("lower(l.trade) = lower(?)")
        args.append(f.trade.strip().replace(" ", "_"))
    if f.town:
        where.append("lower(l.town) = lower(?)")
        args.append(f.town.strip())
    statuses = [s for s in f.website if s != "unchecked"]
    web_parts = []
    if statuses:
        web_parts.append(f"l.website_status IN ({','.join('?' * len(statuses))})")
        args.extend(statuses)
    if "unchecked" in f.website:
        web_parts.append("l.website_status IS NULL")
    if web_parts:
        where.append("(" + " OR ".join(web_parts) + ")")
    return " AND ".join(where), args


def segment_counts(conn: sqlite3.Connection, f: Filters) -> dict[str, int]:
    """How many leads match, how many have an email, and how many are still un-emailed."""
    where, args = _segment_sql(f)
    row = conn.execute(
        f"""SELECT COUNT(*) AS matched,
                   SUM(CASE WHEN l.email IS NOT NULL AND l.email != '' THEN 1 ELSE 0 END) AS with_email,
                   SUM(CASE WHEN l.email IS NOT NULL AND l.email != '' AND NOT EXISTS (
                       SELECT 1 FROM emails e WHERE e.lead_id=l.id AND e.kind='initial' AND e.status != 'deleted')
                       THEN 1 ELSE 0 END) AS available
            FROM leads l WHERE {where}""",
        args,
    ).fetchone()
    return {k: int(row[k] or 0) for k in ("matched", "with_email", "available")}


def _candidates(conn: sqlite3.Connection, f: Filters, limit: int) -> list[dict[str, Any]]:
    where, args = _segment_sql(f)
    rows = conn.execute(
        f"""SELECT l.* FROM leads l WHERE {where} AND l.email IS NOT NULL AND l.email != ''
            AND NOT EXISTS (SELECT 1 FROM emails e WHERE e.lead_id=l.id AND e.kind='initial' AND e.status != 'deleted')
            ORDER BY l.score DESC, l.id LIMIT ?""",
        (*args, limit),
    ).fetchall()
    return [db.row_to_dict(r) for r in rows]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Create / extend
# ---------------------------------------------------------------------------

@dataclass
class DraftResult:
    campaign_id: int
    drafted: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    blocked: dict[str, int] = field(default_factory=dict)  # block reason -> count (among new drafts)
    samples: list[dict[str, Any]] = field(default_factory=list)


def default_templates(country: str) -> tuple[str, str]:
    if country == "US":
        return "cold_email_us.txt", "followup_email_us.txt"
    return "cold_email.txt", "followup_email.txt"


def _read_template(settings: Settings, name: str) -> str:
    path = settings.template_file(name)
    if not path.exists():
        raise CampaignError(f"Template '{name}' not found in the templates folder.")
    return path.read_text(encoding="utf-8")


def create(
    conn: sqlite3.Connection,
    settings: Settings,
    user_id: int | None,
    name: str,
    filters: Filters,
    template: str | None = None,
    followup_template: str | None = None,
    limit: int = 200,
) -> DraftResult:
    name = " ".join((name or "").split())
    if not name:
        raise CampaignError("Give the campaign a name, e.g. \"hvac-austin-oct\".")
    if conn.execute("SELECT 1 FROM campaigns WHERE name=? COLLATE NOCASE", (name,)).fetchone():
        raise CampaignError(f"A campaign called '{name}' already exists. Use `campaign add` to top it up.")
    t_default, f_default = default_templates(filters.country)
    template = template or t_default
    followup_template = followup_template or f_default
    _read_template(settings, template)  # fail early on a typo
    _read_template(settings, followup_template)
    cur = conn.execute(
        "INSERT INTO campaigns(name, country, filters_json, template, followup_template, status, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (name, filters.country, filters.to_json(), template, followup_template, "draft", user_id, now_db()),
    )
    db.commit(conn)
    cid = int(cur.lastrowid)
    log_activity(conn, user_id, "campaign_create", f"Created campaign {name}", None, "Campaign")
    return extend(conn, settings, user_id, cid, limit)


def extend(conn: sqlite3.Connection, settings: Settings, user_id: int | None, campaign: str | int,
           limit: int = 200) -> DraftResult:
    """Draft emails for leads that match the campaign and haven't been emailed yet."""
    c = get(conn, campaign)
    f: Filters = c["filters"]
    template = _read_template(settings, c["template"])
    res = DraftResult(campaign_id=c["id"], counts=segment_counts(conn, f))
    for lead in _candidates(conn, f, limit * 3):
        if res.drafted >= limit:
            break
        if emailer.is_suppressed(conn, lead["email"]):
            res.skipped.append((lead["name"], "address is on the suppression list"))
            continue
        try:
            r = emailer.render(template, lead, settings)
        except EmailError as e:
            raise CampaignError(str(e)) from None
        if r.words > emailer.MAX_WORDS:
            raise CampaignError(f"The template renders to {r.words} words for {lead['name']}; keep it under "
                                f"{emailer.MAX_WORDS}.")
        eid = emailer._create_draft(conn, lead, r, "initial", user_id, c["id"])
        res.drafted += 1
        e = emailer.get_email(conn, eid)
        reason = emailer.block_reason_for(conn, e, settings)
        if reason:
            res.blocked[reason] = res.blocked.get(reason, 0) + 1
        elif len(res.samples) < 3:
            res.samples.append(e)
    return res


def draft_followups(conn: sqlite3.Connection, settings: Settings, user_id: int | None, campaign: str | int,
                    limit: int = 200) -> emailer.DraftReport:
    c = get(conn, campaign)
    template = _read_template(settings, c["followup_template"] or default_templates(c["country"])[1])
    return emailer.followup(conn, settings, user_id, limit, campaign_id=c["id"], template=template)


# ---------------------------------------------------------------------------
# Approve / send / pause
# ---------------------------------------------------------------------------

@dataclass
class ApproveResult:
    approved: int = 0
    blocked: dict[str, int] = field(default_factory=dict)


def pending(conn: sqlite3.Connection, campaign_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM emails WHERE campaign_id=? AND status='pending_review' ORDER BY id", (campaign_id,)
    ).fetchall()]


def approve(conn: sqlite3.Connection, settings: Settings, user_id: int | None, campaign: str | int) -> ApproveResult:
    """Approve every pending draft in the campaign that passes the send rules. Blocked ones stay pending."""
    c = get(conn, campaign)
    res = ApproveResult()
    ts = now_db()
    with db.transaction(conn):
        for e in pending(conn, c["id"]):
            reason = emailer.block_reason_for(conn, e, settings)
            if reason:
                res.blocked[reason] = res.blocked.get(reason, 0) + 1
                continue
            conn.execute("UPDATE emails SET status='approved', approved_by=?, approved_at=? WHERE id=?",
                         (user_id, ts, e["id"]))
            res.approved += 1
        conn.execute("UPDATE campaigns SET status='active', approved_by=?, approved_at=? WHERE id=?",
                     (user_id, ts, c["id"]))
    log_activity(conn, user_id, "campaign_approve", f"Approved {res.approved} email(s) in {c['name']}", None, "Approved")
    return res


def pause(conn: sqlite3.Connection, user_id: int | None, campaign: str | int) -> int:
    """Stop sending: approved-but-unsent emails go back to pending review."""
    c = get(conn, campaign)
    cur = conn.execute("UPDATE emails SET status='pending_review' WHERE campaign_id=? AND status='approved'", (c["id"],))
    conn.execute("UPDATE campaigns SET status='paused' WHERE id=?", (c["id"],))
    db.commit(conn)
    log_activity(conn, user_id, "campaign_pause", f"Paused {c['name']}", None, "Paused")
    return cur.rowcount


def send(conn: sqlite3.Connection, settings: Settings, clients: Any, user_id: int | None, campaign: str | int,
         dry_run: bool = False, **kw: Any) -> emailer.SendReport:
    c = get(conn, campaign)
    if c["status"] == "paused":
        raise CampaignError(f"'{c['name']}' is paused. Run `campaign approve` to resume it.")
    return emailer.send(conn, settings, clients, user_id, dry_run=dry_run, campaign_id=c["id"], **kw)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def band(value: float, bands: list[tuple[float, str]]) -> str:
    label = bands[0][1]
    for lower, name in bands:
        if value >= lower:
            label = name
    return label


def stats(conn: sqlite3.Connection, campaign_id: int) -> dict[str, Any]:
    """Funnel for one campaign. Rates are per 100 first emails delivered (sent minus bounced).

    'Meetings' = campaign leads that got a meeting booked after their first campaign email, by any
    route (they replied and you called, or they rang you). Leads you were also calling may count."""
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0] or 0  # noqa: E731
    cid = campaign_id
    sent = q("SELECT COUNT(*) FROM emails WHERE campaign_id=? AND kind='initial' AND status IN ('sent','bounced')", cid)
    bounced = q("SELECT COUNT(*) FROM emails WHERE campaign_id=? AND kind='initial' AND status='bounced'", cid)
    followups = q("SELECT COUNT(*) FROM emails WHERE campaign_id=? AND kind='followup' AND status IN ('sent','bounced')", cid)
    queued = q("SELECT COUNT(*) FROM emails WHERE campaign_id=? AND status='approved'", cid)
    pending_n = q("SELECT COUNT(*) FROM emails WHERE campaign_id=? AND status='pending_review'", cid)
    first_send = """(SELECT MIN(e.sent_at) FROM emails e WHERE e.lead_id=l.id AND e.campaign_id=? AND e.kind='initial'
                     AND e.status IN ('sent','bounced'))"""
    leads_sql = "SELECT DISTINCT lead_id FROM emails WHERE campaign_id=? AND kind='initial' AND status IN ('sent','bounced')"
    replied = q(f"SELECT COUNT(*) FROM leads l WHERE l.id IN ({leads_sql}) AND l.email_replied=1", cid)
    meetings = q(
        f"""SELECT COUNT(*) FROM leads l WHERE l.id IN ({leads_sql}) AND (
                EXISTS (SELECT 1 FROM meetings m WHERE m.lead_id=l.id AND m.created_at >= {first_send})
                OR (l.status IN ('meeting_booked','proposal_sent','won') AND l.stage_changed_at >= {first_send}))""",
        cid, cid, cid,
    )
    won = q(f"SELECT COUNT(*) FROM leads l WHERE l.id IN ({leads_sql}) AND l.status='won'", cid)
    unsubscribed = q(
        f"""SELECT COUNT(*) FROM leads l JOIN suppression s ON s.email = lower(
                (SELECT e.to_email FROM emails e WHERE e.lead_id=l.id AND e.campaign_id=? AND e.kind='initial' LIMIT 1))
            WHERE l.id IN ({leads_sql}) AND s.reason != 'Bounced'""",
        cid, cid,
    )
    delivered = max(0, sent - bounced)
    pct = lambda n: round(100.0 * n / delivered, 2) if delivered else 0.0  # noqa: E731
    reply_rate, meeting_rate = pct(replied), pct(meetings)
    return {
        "sent": sent, "bounced": bounced, "delivered": delivered, "followups": followups,
        "queued": queued, "pending": pending_n, "replied": replied, "meetings": meetings, "won": won,
        "unsubscribed": unsubscribed,
        "bounce_rate": round(100.0 * bounced / sent, 2) if sent else 0.0,
        "reply_rate": reply_rate, "meeting_rate": meeting_rate,
        "reply_band": band(reply_rate, REPLY_BANDS) if delivered else "-",
        "meeting_band": band(meeting_rate, MEETING_BANDS) if delivered else "-",
        "emails_per_meeting": round(delivered / meetings) if meetings else None,
    }


def health_warnings(s: dict[str, Any]) -> list[str]:
    """Plain-English warnings once there's enough data to mean something."""
    out = []
    if s["sent"] >= 20 and s["bounce_rate"] > 2:
        out.append(f"Bounce rate is {s['bounce_rate']}%. Above 2% hurts your sender reputation: "
                   "pause and check where the addresses came from.")
    if s["delivered"] >= 200 and s["replied"] == 0:
        out.append("200+ delivered and no replies: you're probably landing in spam. Check the domain's "
                   "SPF/DKIM/DMARC and send a test to a Gmail and an Outlook address.")
    if s["delivered"] >= 50 and s["unsubscribed"] * 100 / max(1, s["delivered"]) > 1:
        out.append("More than 1% opted out: the targeting or the message isn't landing.")
    return out
