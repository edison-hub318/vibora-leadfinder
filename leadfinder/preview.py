"""One-page demo websites for leads ("Preview Sites").

Only the lead's name, trade, town and phone number are used, plus generic copy for the trade.
No Google review text or photos (Google's terms don't allow reusing them).
"""
from __future__ import annotations

import io
import re
import shutil
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db, phones
from .clients import Clients, ExternalError
from .config import REPO_ROOT, Settings, load_towns
from .leads import LeadError, require_lead
from .search import haversine_km
from .timeutil import now_london
from .trades import Trade, load_trades, normalise_text
from .users import log_activity

FAMILY_COPY: dict[str, dict[str, Any]] = {
    "trades": {
        "tagline": "Reliable, tidy work from a local {trade_singular} you can trust.",
        "services": ["Repairs and call-outs", "New installations", "Maintenance", "Free quotes"],
        "why": [
            ("Local and reliable", "We turn up when we say we will, and keep you updated."),
            ("Clear, fair pricing", "A written quote before any work starts. No surprises."),
            ("Tidy and careful", "We treat your home like our own and clean up after."),
            ("Fully insured", "Peace of mind on every job, big or small."),
        ],
        "cta": "Call for a free quote",
    },
    "beauty": {
        "tagline": "Look and feel your best, right here in {town}.",
        "services": ["Signature treatments", "Walk-ins welcome", "Gift vouchers", "Special occasions"],
        "why": [
            ("Experienced team", "Skilled, friendly people who take their time with you."),
            ("Relaxed setting", "A calm, welcoming space to unwind."),
            ("Quality products", "Professional products for results that last."),
            ("Easy booking", "Call or message and we'll find a time that suits."),
        ],
        "cta": "Call to book",
    },
    "food": {
        "tagline": "Fresh, made with care, and loved by {town}.",
        "services": ["Freshly made daily", "Order ahead", "Local favourites", "Catering and events"],
        "why": [
            ("Made fresh", "Good ingredients, prepared properly, every day."),
            ("Friendly service", "A warm welcome whether you're a regular or new."),
            ("Local through and through", "Proudly part of the {town} community."),
            ("Easy ordering", "Give us a call and we'll have it ready."),
        ],
        "cta": "Call to order",
    },
    "professional": {
        "tagline": "Straightforward, expert help for people and businesses in {town}.",
        "services": ["Free initial chat", "Clear fixed fees", "Ongoing support", "Flexible appointments"],
        "why": [
            ("Expert and qualified", "Years of experience, explained in plain English."),
            ("Personal service", "You'll always deal with someone who knows you."),
            ("Transparent fees", "You'll know the cost upfront."),
            ("Local", "Based in {town}, and happy to meet in person."),
        ],
        "cta": "Call for a free chat",
    },
    "generic": {
        "tagline": "Friendly, dependable service in {town} and nearby.",
        "services": ["Our core service", "Free quotes", "Flexible times", "Local area covered"],
        "why": [
            ("Local", "Based in {town} and proud of it."),
            ("Reliable", "We do what we say, when we say."),
            ("Fair prices", "Honest pricing with no hidden extras."),
            ("Friendly", "Happy to answer any questions."),
        ],
        "cta": "Call us today",
    },
}

PALETTES = {
    "trades": {"bg": "#0f1720", "accent": "#f5a524", "ink": "#0f1720", "soft": "#f3f5f7"},
    "beauty": {"bg": "#2a1f24", "accent": "#d9a6b3", "ink": "#2a1f24", "soft": "#faf5f6"},
    "food": {"bg": "#22301f", "accent": "#e8b04a", "ink": "#22301f", "soft": "#f8f6f0"},
    "professional": {"bg": "#14213d", "accent": "#7fb3d5", "ink": "#14213d", "soft": "#f4f6fa"},
    "generic": {"bg": "#1d1d1f", "accent": "#5ac8a8", "ink": "#1d1d1f", "soft": "#f5f5f7"},
}


def slugify(text: str) -> str:
    s = normalise_text(text).replace(" ", "-")
    return re.sub(r"-+", "-", s).strip("-")[:60] or "business"


def _singular(label: str) -> str:
    low = label.lower()
    if low == "painters & decorators":
        return "painter and decorator"
    if low.endswith("men"):
        return low[:-3] + "man"
    if low.endswith("ies"):
        return low[:-3] + "y"
    if low.endswith("s"):
        return low[:-1]
    return low


def _env(settings: Settings) -> Environment:
    dirs = [settings.templates_dir / "previews", REPO_ROOT / "templates" / "previews"]
    return Environment(
        loader=FileSystemLoader([str(d) for d in dirs if d.exists()]),
        autoescape=select_autoescape(["html", "j2"]),
    )


def nearby_town_names(settings: Settings, town: str, limit: int = 6) -> list[str]:
    towns = load_towns(settings)
    centre = next((t for t in towns if normalise_text(t["name"]) == normalise_text(town)), None)
    names = [town]
    if centre:
        near = sorted(
            (t for t in towns if t is not centre and not any(c.isdigit() for c in t["name"])),
            key=lambda t: haversine_km(centre["lat"], centre["lng"], t["lat"], t["lng"]),
        )
        names += [t["name"] for t in near if haversine_km(centre["lat"], centre["lng"], t["lat"], t["lng"]) < 15][: limit - 1]
    return names


def render_preview(settings: Settings, lead: dict[str, Any]) -> str:
    trades = load_trades(settings)
    trade: Trade | None = trades.get(lead.get("trade") or "")
    family = trade.family if trade else "generic"
    label = trade.label if trade else (lead.get("trade") or "local business").replace("_", " ")
    copy = FAMILY_COPY[family]
    town = lead.get("town") or "your area"
    fill = {"town": town, "trade": label.lower(), "trade_singular": _singular(label)}
    services = (trade.services if trade and trade.services else copy["services"])
    template = _env(settings).get_template(f"{family}.html.j2")
    phone = phones.pretty(lead.get("phone")) or ""
    tel = phones.classify(lead.get("phone")).e164 or ""
    return template.render(
        name=lead["name"],
        trade_label=label,
        trade_singular=fill["trade_singular"],
        town=town,
        phone=phone,
        tel=tel,
        tagline=copy["tagline"].format(**fill),
        services=services,
        why=[(h.format(**fill), b.format(**fill)) for h, b in copy["why"]],
        cta=copy["cta"],
        areas=nearby_town_names(settings, town),
        palette=PALETTES[family],
        year=now_london().year,
        family=family,
    )


def preview_dir(settings: Settings, slug: str) -> Path:
    return settings.previews_dir / slug


def generate(conn: sqlite3.Connection, settings: Settings, lead_id: int, user_id: int | None) -> Path:
    lead = require_lead(conn, lead_id)
    slug = lead.get("preview_slug") or f"{slugify(lead['name'])}-{lead['id']}"
    folder = preview_dir(settings, slug)
    folder.mkdir(parents=True, exist_ok=True)
    html = render_preview(settings, lead)
    (folder / "index.html").write_text(html, encoding="utf-8")
    conn.execute("UPDATE leads SET preview_slug=? WHERE id=?", (slug, lead_id))
    db.commit(conn)
    log_activity(conn, user_id, "preview", "Generated preview site", lead_id, "Preview")
    return folder / "index.html"


def delete(conn: sqlite3.Connection, settings: Settings, lead_id: int, user_id: int | None) -> None:
    lead = require_lead(conn, lead_id)
    if lead.get("preview_slug"):
        folder = preview_dir(settings, lead["preview_slug"])
        if folder.exists() and folder.resolve().parent == settings.previews_dir.resolve():
            shutil.rmtree(folder)
    conn.execute("UPDATE leads SET preview_slug=NULL, preview_url=NULL WHERE id=?", (lead_id,))
    db.commit(conn)
    log_activity(conn, user_id, "preview_delete", "Deleted preview site", lead_id)


def list_previews(conn: sqlite3.Connection, settings: Settings) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, trade, town, preview_slug, preview_url FROM leads WHERE preview_slug IS NOT NULL ORDER BY id DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["exists"] = (preview_dir(settings, r["preview_slug"]) / "index.html").exists()
        out.append(d)
    return out


def export_zip(conn: sqlite3.Connection, settings: Settings, lead_id: int) -> tuple[str, bytes]:
    lead = require_lead(conn, lead_id)
    if not lead.get("preview_slug"):
        raise LeadError("Generate a preview first.")
    folder = preview_dir(settings, lead["preview_slug"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in folder.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(folder).as_posix())
    return f"{lead['preview_slug']}.zip", buf.getvalue()


def publish(conn: sqlite3.Connection, settings: Settings, clients: Clients, lead_id: int, user_id: int | None) -> str:
    if clients.publisher is None:
        raise LeadError("Set NETLIFY_AUTH_TOKEN in .env to publish previews.")
    lead = require_lead(conn, lead_id)
    if not lead.get("preview_slug"):
        generate(conn, settings, lead_id, user_id)
        lead = require_lead(conn, lead_id)
    folder = preview_dir(settings, lead["preview_slug"])
    try:
        site_id, url = clients.publisher.deploy(folder, lead.get("netlify_site_id"), lead["preview_slug"])
    except ExternalError as e:
        raise LeadError(str(e)) from None
    conn.execute("UPDATE leads SET preview_url=?, netlify_site_id=? WHERE id=?", (url, site_id, lead_id))
    db.commit(conn)
    log_activity(conn, user_id, "preview_publish", f"Published preview: {url}", lead_id, "Published")
    return url


