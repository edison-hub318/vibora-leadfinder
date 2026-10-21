"""Enrichment: business email addresses from the lead's own website, and the Companies House
check that decides whether a lead is a limited company (which matters for PECR email rules)."""
from __future__ import annotations

import re
import sqlite3
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urljoin

from . import db
from .audit import EMAIL_RE, _normalise_url, _Robots, classify_domain, host_of, parse_page
from .clients import Clients, ExternalError
from .config import Settings, load_website_rules
from .trades import name_key, normalise_text
from .users import log_activity

PREFERRED_PREFIXES = ("info", "hello", "contact", "enquiries", "enquiry", "office", "admin", "sales", "bookings", "mail")
BAD_EMAIL_PARTS = (
    "example.com", "domain.com", "email.com", "yourdomain", "sentry", "wixpress.com", "@2x", ".png", ".jpg",
    ".jpeg", ".gif", ".svg", ".webp", "godaddy.com", "noreply", "no-reply", "donotreply", "privacy@", "abuse@",
    "postmaster@", "webmaster@", "name@", "user@", "test@", "squarespace.com", "wordpress.com",
)

POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)


US_ZIP_RE = re.compile(r"\b[A-Z]{2}\s+(\d{5})(?:-\d{4})?\b")


def extract_postcode(address: str | None, country: str = "GB") -> str:
    """UK postcode ("RH10 1AA") or, for US addresses, the 5-digit ZIP after the state code."""
    if not address:
        return ""
    if country == "US":
        zips = US_ZIP_RE.findall(address.upper())
        return zips[-1] if zips else ""
    found = POSTCODE_RE.findall(address.upper())
    if not found:
        return ""
    out, inn = found[-1]
    return f"{out} {inn}"


def _clean_emails(candidates: list[str]) -> list[str]:
    seen: list[str] = []
    for e in candidates:
        e = e.strip().strip(".,;:()<>[]'\"").lower()
        if not EMAIL_RE.fullmatch(e):
            continue
        if any(b in e for b in BAD_EMAIL_PARTS):
            continue
        if e not in seen:
            seen.append(e)
    return seen


def rank_emails(emails: list[str], site_host: str) -> list[str]:
    def key(e: str) -> tuple[int, int]:
        local, _, dom = e.partition("@")
        same = 0 if (site_host and (dom == site_host or site_host.endswith("." + dom) or dom.endswith(site_host))) else 1
        pref = 0 if local in PREFERRED_PREFIXES else 1
        return (same, pref)

    return sorted(emails, key=key)


def find_email(website: str | None, clients: Clients, rules: dict[str, Any]) -> tuple[str | None, str | None]:
    """Fetch the homepage and the contact page and return (email, url where it was found).
    Only addresses visibly published on the business's own site are returned - never guessed."""
    if not website:
        return None, None
    kind, _ = classify_domain(website, rules)
    if kind == "social_only":
        return None, None
    home = _normalise_url(website)
    robots = _Robots(clients.fetcher)
    pages: list[str] = []
    if robots.allowed(home):
        res = clients.fetcher.get(home, timeout=15)
        if res.ok:
            final = res.final_url or home
            page = parse_page(res.text)
            found = _clean_emails(page.mailto + EMAIL_RE.findall(page.text))
            if found:
                return rank_emails(found, host_of(final))[0], final
            contact = next((h for h, t in page.links if "contact" in (h + " " + t).lower()), None)
            pages.append(urljoin(final, contact) if contact else urljoin(final, "/contact"))
            home = final
    if not pages:
        pages.append(urljoin(home, "/contact"))
    for url in pages:
        if host_of(url) != host_of(home) or not robots.allowed(url):
            continue
        res = clients.fetcher.get(url, timeout=15)
        if res.ok:
            page = parse_page(res.text)
            found = _clean_emails(page.mailto + EMAIL_RE.findall(page.text))
            if found:
                return rank_emails(found, host_of(home))[0], res.final_url or url
    return None, None


def find_emails_command(
    conn: sqlite3.Connection, settings: Settings, clients: Clients, user_id: int | None, limit: int = 20
) -> list[dict[str, Any]]:
    rules = load_website_rules(settings)
    rows = conn.execute(
        """SELECT * FROM leads WHERE duplicate_of IS NULL AND website IS NOT NULL AND website != ''
           AND (email IS NULL OR email = '') AND COALESCE(website_status,'') != 'social_only'
           AND status NOT IN ('dnc','won','lost') ORDER BY score DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        email, source = find_email(r["website"], clients, rules)
        if email:
            conn.execute("UPDATE leads SET email=?, email_source=? WHERE id=?", (email, source, r["id"]))
            db.commit(conn)
            log_activity(conn, user_id, "email_found", f"Found {email}", r["id"])
        out.append({"id": r["id"], "name": r["name"], "email": email, "source": source})
    return out


# ---------------------------------------------------------------------------
# Companies House
# ---------------------------------------------------------------------------

_CO_SUFFIX = re.compile(r"\b(ltd|limited|llp|plc|l t d|co uk|uk)\b")


def _co_name(s: str) -> str:
    return re.sub(r"\s+", " ", _CO_SUFFIX.sub(" ", normalise_text(s))).strip()


def names_match(a: str, b: str) -> bool:
    x, y = _co_name(a), _co_name(b)
    if not x or not y:
        return False
    if x == y or name_key(a) == name_key(b):
        return True
    return SequenceMatcher(None, x, y).ratio() >= 0.9


def match_company(lead: dict[str, Any], items: list[dict[str, Any]]) -> tuple[str, str | None]:
    """Decide the entity type from Companies House search results.
    Returns ('limited_company', number) | ('likely_sole_trader', None) | ('unknown', None)."""
    live = [i for i in items if (i.get("company_status") or "active") in ("active", "open")]
    name_hits = [i for i in live if names_match(lead.get("name", ""), i.get("title", ""))]
    if not name_hits:
        return "likely_sole_trader", None
    pc = (lead.get("postcode") or extract_postcode(lead.get("address"))).replace(" ", "").upper()
    if pc:
        pc_hits = []
        for i in name_hits:
            addr = i.get("address") or {}
            ipc = (addr.get("postal_code") or extract_postcode(i.get("address_snippet", ""))).replace(" ", "").upper()
            if ipc and ipc == pc:
                pc_hits.append(i)
        if len(pc_hits) == 1:
            return "limited_company", pc_hits[0].get("company_number")
    return "unknown", None


def classify_lead(lead: dict[str, Any], clients: Clients) -> tuple[str, str | None]:
    if clients.companies_house is None:
        return lead.get("entity_type") or "unknown", None
    try:
        items = clients.companies_house.search(lead.get("name", ""))
    except ExternalError:
        return lead.get("entity_type") or "unknown", None
    return match_company(lead, items)


def classify_command(
    conn: sqlite3.Connection, clients: Clients, user_id: int | None, limit: int = 50
) -> dict[str, int]:
    counts = {"limited_company": 0, "likely_sole_trader": 0, "unknown": 0}
    if clients.companies_house is None:
        return counts
    rows = conn.execute(
        """SELECT * FROM leads WHERE duplicate_of IS NULL AND COALESCE(entity_source,'') NOT IN ('manual','companies_house')
           ORDER BY score DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    for r in rows:
        lead = dict(r)
        etype, number = classify_lead(lead, clients)
        if etype == "unknown" and lead.get("phone_type") == "mobile":
            etype = "likely_sole_trader"
        counts[etype] = counts.get(etype, 0) + 1
        conn.execute(
            "UPDATE leads SET entity_type=?, entity_source='companies_house', company_number=? WHERE id=?",
            (etype, number, r["id"]),
        )
    db.commit(conn)
    return counts


def set_type(conn: sqlite3.Connection, lead_id: int, entity: str, user_id: int | None) -> None:
    mapping = {"limited_company": "limited_company", "sole_trader": "sole_trader", "ltd": "limited_company"}
    if entity not in mapping:
        raise ValueError("Type must be limited_company or sole_trader")
    conn.execute(
        "UPDATE leads SET entity_type=?, entity_source='manual' WHERE id=?", (mapping[entity], lead_id)
    )
    db.commit(conn)
    log_activity(conn, user_id, "set_type", f"Marked as {mapping[entity].replace('_', ' ')}", lead_id)
