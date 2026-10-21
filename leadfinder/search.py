"""The search pipeline. `--max N` means N qualified, new, callable leads - not N raw results.

Steps (each reported in the summary):
 1. lock to the area (geocode once, cached; locationBias circle; drop results > radius x 1.5)
 2. widen the query with synonyms, then nearby towns, until N leads or the budget runs out
 3. category check          4. chains/franchises       5. duplicate merge
 6. skip already-handled    7. phone check             8. quality flags
 9. website audit           10. enrichment (emails, Companies House)
11. score, assign owner, save in one transaction
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from difflib import SequenceMatcher
from typing import Any, Callable

from . import db, phones
from .audit import apply_result, audit_many, classify_domain, make_hook, shareable_domain
from .clients import GEOCODE_FIELD_MASK, TEXT_SEARCH_FIELD_MASK, Clients, ExternalError
from .config import Settings, load_towns, load_website_rules
from .enrich import classify_lead, extract_postcode, find_email
from .freetier import TEXT_SEARCH, TEXT_SEARCH_PRO, AllowanceReached, UsageGuard
from .scoring import score_lead
from .timeutil import from_db, now_db, now_utc, to_db
from .trades import ChainMatcher, Trade, name_key, normalise_text, resolve_trade, trade_matches
from .users import User

CLOSED_STATUSES = ("won", "lost", "not_interested", "wrong_number", "dnc")
PAGES_PER_QUERY = 3  # Google returns at most 60 results (3 x 20) per query
REPEAT_DAYS = 14


class SearchError(Exception):
    pass


class TownNotFound(SearchError):
    pass


class RepeatSearch(SearchError):
    """The same trade + town was searched recently; re-run with force=True."""


@dataclass
class SearchOptions:
    trade: str
    town: str
    max_leads: int = 60
    radius_km: float = 8.0
    include_no_phone: bool = False
    audit: bool = True
    force: bool = False
    request_cap: int | None = None
    country: str = "GB"  # GB or US


@dataclass
class QueryPlan:
    text: str
    area: str
    lat: float
    lng: float


@dataclass
class SearchReport:
    search_id: int
    trade: str
    town: str
    requested: int
    queries: list[str] = field(default_factory=list)
    funnel: list[tuple[str, int]] = field(default_factory=list)
    handled: dict[str, int] = field(default_factory=dict)
    closed_skipped: int = 0
    website: dict[str, int] = field(default_factory=dict)
    requests_made: int = 0
    est_cost_gbp: float = 0.0
    stop_reason: str = ""
    stop_message: str = ""
    new_leads: list[dict[str, Any]] = field(default_factory=list)
    known_count: int = 0
    raw_count: int = 0
    ai_calls: int = 0

    @property
    def qualified(self) -> int:
        return len(self.new_leads)

    @property
    def top(self) -> list[dict[str, Any]]:
        return sorted(self.new_leads, key=lambda d: -d.get("score", 0))[:10]


# ---------------------------------------------------------------------------
# Geometry and geocoding
# ---------------------------------------------------------------------------

# Countries the search supports: what to append to queries, and Google's region code.
COUNTRIES: dict[str, dict[str, str]] = {
    "GB": {"label": "UK", "suffix": "UK", "region": "GB"},
    "US": {"label": "USA", "suffix": "USA", "region": "US"},
}


def norm_country(country: str | None) -> str:
    c = (country or "GB").strip().upper()
    c = {"UK": "GB", "USA": "US", "UNITED STATES": "US", "UNITED KINGDOM": "GB"}.get(c, c)
    if c not in COUNTRIES:
        raise SearchError(f"Country '{country}' isn't supported. Use GB (UK) or US.")
    return c


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def town_key(town: str, country: str = "GB") -> str:
    # US towns get a prefix so "Manchester" (UK) and "Manchester, NH" never share a cache row.
    key = normalise_text(town)
    return key if norm_country(country) == "GB" else f"{country.lower()}:{key}"


def tidy_town(town: str) -> str:
    t = " ".join(town.strip().split())
    # Postcode districts stay upper case; place names get title case.
    if any(c.isdigit() for c in t):
        return t.upper()
    return " ".join(w if w.isupper() and len(w) > 1 else w.capitalize() for w in t.split())


def geocode_town(
    conn: sqlite3.Connection,
    settings: Settings,
    clients: Clients,
    guard: UsageGuard,
    town: str,
    user_id: int | None,
    search_id: int | None,
    country: str = "GB",
) -> dict[str, Any]:
    """Coordinates for a town (cached forever). Costs at most one request per new town."""
    country = norm_country(country)
    key = town_key(town, country)
    row = conn.execute("SELECT * FROM towns WHERE name_key=?", (key,)).fetchone()
    if row is not None:
        if row["not_found"]:
            raise TownNotFound(f"Google couldn't find '{town}'. Check the spelling, or try a nearby town or postcode district.")
        return dict(row)
    for t in (load_towns(settings) if country == "GB" else []):
        if town_key(t["name"]) == key and t.get("lat") is not None:
            conn.execute(
                "INSERT INTO towns(name_key, display_name, lat, lng, formatted_address, created_at) VALUES (?,?,?,?,?,?)",
                (key, t["name"], float(t["lat"]), float(t["lng"]), f"{t['name']}, UK", now_db()),
            )
            db.commit(conn)
            return dict(conn.execute("SELECT * FROM towns WHERE name_key=?", (key,)).fetchone())
    guard.check(TEXT_SEARCH_PRO)
    cinfo = COUNTRIES[country]
    body = {"textQuery": f"{town}, {cinfo['suffix']}", "regionCode": cinfo["region"], "pageSize": 1}
    resp = clients.places.text_search(body, GEOCODE_FIELD_MASK)
    guard.record(TEXT_SEARCH_PRO, user_id, search_id)
    places = resp.get("places") or []
    loc = (places[0].get("location") or {}) if places else {}
    if not places or loc.get("latitude") is None:
        conn.execute(
            "INSERT INTO towns(name_key, display_name, not_found, created_at) VALUES (?,?,1,?)",
            (key, tidy_town(town), now_db()),
        )
        db.commit(conn)
        raise TownNotFound(f"Google couldn't find '{town}'. Check the spelling, or try a nearby town or postcode district.")
    conn.execute(
        "INSERT INTO towns(name_key, display_name, lat, lng, formatted_address, created_at) VALUES (?,?,?,?,?,?)",
        (key, tidy_town(town), loc["latitude"], loc["longitude"], places[0].get("formattedAddress", ""), now_db()),
    )
    db.commit(conn)
    return dict(conn.execute("SELECT * FROM towns WHERE name_key=?", (key,)).fetchone())


def nearby_areas(
    conn: sqlite3.Connection, settings: Settings, centre: dict[str, Any], radius_km: float, country: str = "GB"
) -> list[dict[str, Any]]:
    """Other towns/postcode districts within the radius (from towns.yaml and the cache, same country)."""
    country = norm_country(country)
    seen = {centre["name_key"]}
    out = []
    candidates = [
        {"name": t["name"], "lat": t["lat"], "lng": t["lng"]}
        for t in (load_towns(settings) if country == "GB" else []) if t.get("lat") is not None
    ]
    for r in conn.execute("SELECT name_key, display_name, lat, lng FROM towns WHERE not_found=0").fetchall():
        is_gb = ":" not in r["name_key"]
        if (country == "GB") == is_gb and (is_gb or r["name_key"].startswith(country.lower() + ":")):
            candidates.append({"name": r["display_name"], "lat": r["lat"], "lng": r["lng"]})
    for c in candidates:
        k = town_key(c["name"], country)
        if k in seen:
            continue
        d = haversine_km(centre["lat"], centre["lng"], float(c["lat"]), float(c["lng"]))
        if 0.5 < d <= radius_km:
            seen.add(k)
            out.append({**c, "distance": d})
    return sorted(out, key=lambda c: c["distance"])


# ---------------------------------------------------------------------------
# Repeat-search warning and planning
# ---------------------------------------------------------------------------

def recent_search(conn: sqlite3.Connection, trade_key: str, town: str, country: str = "GB") -> dict[str, Any] | None:
    since = to_db(now_utc() - timedelta(days=REPEAT_DAYS))
    row = conn.execute(
        """SELECT s.*, u.full_name FROM searches s LEFT JOIN users u ON u.id = s.user_id
           WHERE lower(s.trade)=lower(?) AND lower(s.town)=lower(?) AND s.created_at >= ? AND s.status='done'
           AND COALESCE(s.country,'GB')=? ORDER BY s.id DESC LIMIT 1""",
        (trade_key, tidy_town(town), since, norm_country(country)),
    ).fetchone()
    return dict(row) if row else None


def repeat_warning(conn: sqlite3.Connection, trade: Trade, town: str, country: str = "GB") -> str | None:
    prev = recent_search(conn, trade.key, town, country)
    if not prev:
        return None
    days = max(0, (now_utc() - from_db(prev["created_at"])).days)  # type: ignore[operator]
    who = (prev.get("full_name") or "Someone").split()[0]
    # Google's results barely change in two weeks; expect roughly 5% fresh listings per week.
    likely = max(0, round(prev["raw_results"] * 0.05 * max(days, 1) / 7))
    when = "today" if days == 0 else f"{days} day{'s' if days != 1 else ''} ago"
    return (
        f"{who} searched {trade.label.lower()} in {tidy_town(town)} {when}, "
        f"{likely} new lead{'s' if likely != 1 else ''} likely."
    )


def planned_requests(
    conn: sqlite3.Connection, guard: UsageGuard, town: str, n: int, cap: int | None = None, country: str = "GB"
) -> dict[str, int]:
    cached = conn.execute("SELECT 1 FROM towns WHERE name_key=?", (town_key(town, country),)).fetchone() is not None
    cap = cap or guard.per_search_cap
    # Roughly a third of raw results survive the filters, 20 results per request.
    est = max(1, min(cap, math.ceil(n / 7)))
    return {"text_search": est, "cap": cap, "geocode": 0 if cached else 1}


# ---------------------------------------------------------------------------
# Place -> lead
# ---------------------------------------------------------------------------

def place_to_lead(place: dict[str, Any], trade: Trade, town: str, country: str = "GB") -> dict[str, Any]:
    loc = place.get("location") or {}
    phone = place.get("nationalPhoneNumber") or ""
    if country == "US" and phone:
        phone = phones.us_storage(phone) or phone
    info = phones.classify(phone, country)
    return {
        "_key": place["id"],
        "place_id": place["id"],
        "name": (place.get("displayName") or {}).get("text") or "Unnamed business",
        "trade": trade.key,
        "town": tidy_town(town),
        "address": place.get("formattedAddress") or "",
        "postcode": extract_postcode(place.get("formattedAddress"), country),
        "country": country,
        "phone": phone,
        "phone_e164": info.e164 or None,
        "phone_type": info.kind,
        "website": place.get("websiteUri") or None,
        "rating": place.get("rating"),
        "review_count": int(place.get("userRatingCount") or 0),
        "business_status": place.get("businessStatus") or "OPERATIONAL",
        "primary_type": place.get("primaryType"),
        "maps_url": place.get("googleMapsUri"),
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
    }


GOOGLE_FIELDS = (
    "name", "address", "postcode", "phone", "phone_e164", "phone_type", "website", "rating",
    "review_count", "business_status", "primary_type", "maps_url", "lat", "lng",
)


# ---------------------------------------------------------------------------
# Qualification (steps 4-7), re-run after every page so the stop rule is exact
# ---------------------------------------------------------------------------

@dataclass
class _DBIndex:
    by_place: dict[str, dict[str, Any]]
    by_phone: dict[str, dict[str, Any]]
    by_postcode: dict[str, list[dict[str, Any]]]
    name_towns: dict[str, set[str]]
    domains: Counter
    dnc: set[str]
    activity: dict[int, str]  # lead id -> last call/stage time


def _load_index(conn: sqlite3.Connection, rules: dict[str, Any]) -> _DBIndex:
    rows = [dict(r) for r in conn.execute("SELECT * FROM leads").fetchall()]
    by_place = {r["place_id"]: r for r in rows}
    primaries = [r for r in rows if r["duplicate_of"] is None]
    by_phone = {r["phone_e164"]: r for r in primaries if r["phone_e164"]}
    by_pc: dict[str, list[dict[str, Any]]] = {}
    name_towns: dict[str, set[str]] = {}
    domains: Counter = Counter()
    for r in rows:
        if r["postcode"] and r["duplicate_of"] is None:
            by_pc.setdefault(r["postcode"].replace(" ", ""), []).append(r)
        if r["town"]:
            name_towns.setdefault(name_key(r["name"]), set()).add(town_key(r["town"]))
    for r in rows:
        d = shareable_domain(r["website"], rules)
        if d:
            domains[(d, r["place_id"])] += 1
    dnc = {r["phone_e164"] for r in conn.execute("SELECT phone_e164 FROM dnc").fetchall()}
    activity: dict[int, str] = {}
    for r in conn.execute(
        "SELECT lead_id, MAX(created_at) AS t FROM (SELECT lead_id, created_at FROM calls UNION ALL "
        "SELECT lead_id, created_at FROM stage_changes) GROUP BY lead_id"
    ).fetchall():
        activity[r["lead_id"]] = r["t"]
    return _DBIndex(by_place, by_phone, by_pc, name_towns, domains, dnc, activity)


def _similar(a: str, b: str) -> bool:
    """Near-identical names ('Smith Plumbing & Heating' vs 'Smith Plumbing and Heating Ltd')."""
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    if re.findall(r"\d+", ka) != re.findall(r"\d+", kb):  # 'Unit 1' vs 'Unit 2' are different
        return False
    return SequenceMatcher(None, ka, kb).ratio() >= 0.92


def _handled_reason(existing: dict[str, Any], idx: _DBIndex, user_id: int | None) -> str:
    """Why a lead that's already in the database isn't new."""
    status = existing["status"]
    if status == "won":
        return "client"
    if status == "dnc" or (existing["phone_e164"] and existing["phone_e164"] in idx.dnc):
        return "dnc"
    if existing["tps_status"] == "registered":
        return "tps"
    if status == "not_interested":
        last = from_db(existing.get("stage_changed_at") or existing.get("last_called_at"))
        if last and now_utc() - last <= timedelta(days=90):
            return "not_interested"
    if existing["owner_id"] and existing["owner_id"] != user_id:
        last = from_db(idx.activity.get(existing["id"]))
        if last and now_utc() - last <= timedelta(days=30):
            return "partner"
    return "known"


@dataclass
class _Qualified:
    qualified: list[dict[str, Any]]
    duplicates: list[tuple[dict[str, Any], Any]]  # (candidate, primary _key or db id)
    blocked: list[dict[str, Any]]  # new listings whose number is on the DNC list
    known: list[tuple[dict[str, Any], dict[str, Any]]]  # (candidate, existing row)
    counts: dict[str, int]
    handled: Counter


def qualify(
    pool: list[dict[str, Any]],
    all_raw: list[dict[str, Any]],
    town: str,
    idx: _DBIndex,
    chains: ChainMatcher,
    rules: dict[str, Any],
    user_id: int | None,
    include_no_phone: bool,
) -> _Qualified:
    counts: dict[str, int] = {}
    handled: Counter = Counter()

    # Step 4: chains and franchises.
    raw_ids = {c["place_id"] for c in all_raw}
    domain_counts: Counter = Counter()
    for (d, pid), n in idx.domains.items():
        if pid not in raw_ids:
            domain_counts[d] += n
    for c in all_raw:
        d = shareable_domain(c.get("website"), rules)
        if d:
            domain_counts[d] += 1
    tk = town_key(town)
    after_chain = []
    for c in pool:
        dom = shareable_domain(c.get("website"), rules)
        towns = set(idx.name_towns.get(name_key(c["name"]), set())) | {tk}
        if chains.match(c["name"]) or (dom and domain_counts[dom] >= 3) or len(towns) >= 3:
            continue
        after_chain.append(c)
    counts["chains"] = len(after_chain)

    # Step 5: duplicates beyond the place id - same phone, or near-identical name at the same postcode.
    duplicates: list[tuple[dict[str, Any], Any]] = []
    remaining: list[dict[str, Any]] = []
    for c in after_chain:
        if c["place_id"] in idx.by_place:
            remaining.append(c)  # it *is* a known lead; step 6 handles it
            continue
        match = None
        if c.get("phone_e164") and c["phone_e164"] in idx.by_phone:
            match = idx.by_phone[c["phone_e164"]]
        elif c.get("postcode"):
            for r in idx.by_postcode.get(c["postcode"].replace(" ", ""), []):
                if _similar(r["name"], c["name"]):
                    match = r
                    break
        if match is not None and match["place_id"] != c["place_id"]:
            duplicates.append((c, match["id"]))
        else:
            remaining.append(c)
    # Within this batch: group, keep the listing with the most reviews.
    groups: list[list[dict[str, Any]]] = []
    for c in remaining:
        placed = False
        for g in groups:
            for o in g:
                same_phone = c.get("phone_e164") and c["phone_e164"] == o.get("phone_e164")
                same_pc = c.get("postcode") and c["postcode"] == o.get("postcode") and _similar(c["name"], o["name"])
                if same_phone or same_pc:
                    g.append(c)
                    placed = True
                    break
            if placed:
                break
        if not placed:
            groups.append([c])
    after_dupes = []
    for g in groups:
        # A known lead in the group stays the primary so ids never change.
        known = [c for c in g if c["place_id"] in idx.by_place]
        primary = known[0] if known else max(g, key=lambda c: (c.get("review_count") or 0))
        after_dupes.append(primary)
        for c in g:
            if c is not primary and c["place_id"] not in idx.by_place:
                ref = idx.by_place[primary["place_id"]]["id"] if known else primary["_key"]
                duplicates.append((c, ref))
    counts["duplicates"] = len(after_dupes)

    # Step 6: anyone already handled (or already in the database at all).
    known: list[tuple[dict[str, Any], dict[str, Any]]] = []
    blocked: list[dict[str, Any]] = []
    fresh = []
    for c in after_dupes:
        existing = idx.by_place.get(c["place_id"])
        if existing is not None:
            handled[_handled_reason(existing, idx, user_id)] += 1
            known.append((c, existing))
        elif c.get("phone_e164") and c["phone_e164"] in idx.dnc:
            handled["dnc"] += 1
            blocked.append(c)
        else:
            fresh.append(c)
    counts["handled"] = len(fresh)

    # Step 7: phone check.
    qualified = []
    for c in fresh:
        kind = c.get("phone_type")
        if kind in ("none", "invalid") and not include_no_phone:
            continue
        flags = set(c.get("quality_flags") or [])
        if kind == "invalid":
            flags.add("invalid_phone")
        c["quality_flags"] = sorted(flags)
        if kind == "mobile":
            c["entity_type"] = "likely_sole_trader"
            c["entity_source"] = "phone"
        qualified.append(c)
    counts["phone"] = len(qualified)
    return _Qualified(qualified, duplicates, blocked, known, counts, handled)


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

ProgressFn = Callable[[str, dict[str, Any]], None]


def run_search(
    conn: sqlite3.Connection,
    settings: Settings,
    clients: Clients,
    user: User | None,
    opts: SearchOptions,
    progress: ProgressFn | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SearchReport:
    emit = progress or (lambda *_: None)
    trade = resolve_trade(settings, opts.trade)
    town = tidy_town(opts.town)
    country = norm_country(opts.country)
    cinfo = COUNTRIES[country]
    uid = user.id if user else None
    n_wanted = max(1, int(opts.max_leads))

    if not opts.force:
        warning = repeat_warning(conn, trade, town, country)
        if warning:
            raise RepeatSearch(warning + " Use --force to search again.")

    guard = UsageGuard(conn, settings)
    rules = load_website_rules(settings)
    chains = ChainMatcher.from_settings(settings)
    cap = opts.request_cap or guard.per_search_cap

    cur = conn.execute(
        "INSERT INTO searches(user_id, trade, town, radius_km, requested, country, created_at) VALUES (?,?,?,?,?,?,?)",
        (uid, trade.key, town, opts.radius_km, n_wanted, country, now_db()),
    )
    search_id = int(cur.lastrowid)
    db.commit(conn)
    report = SearchReport(search_id=search_id, trade=trade.label, town=town, requested=n_wanted)

    def finish_failed(reason: str, message: str) -> None:
        conn.execute(
            "UPDATE searches SET status='failed', stop_reason=?, finished_at=? WHERE id=?",
            (f"{reason}: {message}", now_db(), search_id),
        )
        db.commit(conn)

    # Step 1: lock to the area.
    try:
        centre = geocode_town(conn, settings, clients, guard, town, uid, search_id, country)
    except TownNotFound as e:
        finish_failed("town_not_found", str(e))
        raise
    except AllowanceReached as e:
        finish_failed("allowance", str(e))
        raise SearchError(str(e)) from None
    except ExternalError as e:
        finish_failed("error", str(e))
        raise SearchError(str(e)) from None

    radius_m = min(50000.0, opts.radius_km * 1000.0)
    max_km = opts.radius_km * 1.5

    # Step 2: the query plan - main query, synonyms, then nearby towns.
    plan: list[QueryPlan] = [QueryPlan(trade.query_text, town, centre["lat"], centre["lng"])]
    for s in trade.synonyms:
        plan.append(QueryPlan(s, town, centre["lat"], centre["lng"]))
    for area in nearby_areas(conn, settings, centre, opts.radius_km, country):
        plan.append(QueryPlan(trade.query_text, area["name"], float(area["lat"]), float(area["lng"])))

    idx = _load_index(conn, rules)
    seen: set[str] = set()
    all_raw: list[dict[str, Any]] = []
    pool: list[dict[str, Any]] = []  # passed area + category
    after_area = 0
    closed = 0
    requests_made = 0
    stop_reason = ""
    stop_message = ""
    result = qualify([], [], town, idx, chains, rules, uid, opts.include_no_phone)

    emit("plan", {"planned": planned_requests(conn, guard, town, n_wanted, cap, country), "queries": len(plan)})

    for q in plan:
        if stop_reason:
            break
        page_token = None
        query_text = f"{q.text} in {q.area}, {cinfo['suffix']}"
        for page in range(PAGES_PER_QUERY):
            if len(result.qualified) >= n_wanted:
                stop_reason = "enough"
                break
            if requests_made >= cap:
                stop_reason = "budget"
                stop_message = f"Per-search budget of {cap} Google requests reached."
                break
            try:
                guard.check(TEXT_SEARCH)
            except AllowanceReached as e:
                stop_reason = "allowance"
                stop_message = str(e)
                break
            body: dict[str, Any] = {
                "textQuery": query_text,
                "regionCode": cinfo["region"],
                "pageSize": 20,
                "locationBias": {"circle": {"center": {"latitude": q.lat, "longitude": q.lng}, "radius": radius_m}},
            }
            if page_token:
                body["pageToken"] = page_token
                sleep(2.0)  # Google needs a moment before the next page token works
            try:
                resp = clients.places.text_search(body, TEXT_SEARCH_FIELD_MASK)
            except ExternalError as e:
                stop_reason = "error"
                stop_message = str(e)
                break
            guard.record(TEXT_SEARCH, uid, search_id)
            requests_made += 1
            if query_text not in report.queries:
                report.queries.append(query_text)

            for place in resp.get("places") or []:
                pid = place.get("id")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                cand = place_to_lead(place, trade, town, country)
                all_raw.append(cand)
                if cand["business_status"] != "OPERATIONAL":
                    closed += 1
                    continue
                if cand["lat"] is not None and haversine_km(centre["lat"], centre["lng"], cand["lat"], cand["lng"]) > max_km:
                    continue
                after_area += 1
                if not trade_matches(trade, cand["name"], cand["primary_type"]):
                    continue
                pool.append(cand)

            result = qualify(pool, all_raw, town, idx, chains, rules, uid, opts.include_no_phone)
            emit("page", {"query": query_text, "page": page + 1, "qualified": len(result.qualified), "requests": requests_made})
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    if not stop_reason:
        stop_reason = "enough" if len(result.qualified) >= n_wanted else "exhausted"
        if stop_reason == "exhausted":
            stop_message = "Google has no more results for these queries and nearby areas."

    qualified = result.qualified

    # Step 8: quality flags.
    for c in qualified:
        flags = set(c.get("quality_flags") or [])
        if c["review_count"] == 0 and not c.get("website"):
            flags.add("possible_ghost")
        if c.get("rating") is not None and c["review_count"] > 0 and float(c["rating"]) < 3.5:
            flags.add("low_rating")
        c["quality_flags"] = sorted(flags)
        c.setdefault("entity_type", "unknown")

    # Step 9: website audit.
    if opts.audit and qualified:
        report.ai_calls = 0
        if clients.ai is not None:
            from .audit import ai_calls_estimate

            report.ai_calls = ai_calls_estimate(qualified, settings, rules)
            emit("ai_plan", {"calls": report.ai_calls})
        results = audit_many(
            qualified, clients, settings, progress=lambda d, t: emit("audit", {"done": d, "total": t})
        )
        for c in qualified:
            apply_result(c, results[c["_key"]])
    else:
        for c in qualified:
            kind, platform = classify_domain(c.get("website"), rules)
            if not c.get("website"):
                c["website_status"], c["website_issues"] = "none", ["No website"]
            elif kind:
                c["website_status"] = kind
                c["website_issues"] = [f"{platform} page only, no real website" if kind == "social_only" else f"Free {platform} site"]
            else:
                c["website_status"], c["website_issues"] = None, []

    # Step 10: enrichment.
    if opts.audit:
        for i, c in enumerate(qualified):
            if c.get("website") and c.get("website_status") != "social_only":
                email, source = find_email(c["website"], clients, rules)
                if email:
                    c["email"], c["email_source"] = email, source
            emit("enrich", {"done": i + 1, "total": len(qualified)})
    if clients.companies_house is not None and country == "GB":  # Companies House is UK-only
        for c in qualified:
            etype, number = classify_lead(c, clients)
            if etype == "unknown" and c.get("phone_type") == "mobile":
                etype = "likely_sole_trader"
            c["entity_type"], c["company_number"], c["entity_source"] = etype, number, "companies_house"

    # Step 11: score, assign, save - one transaction.
    for c in qualified:
        c["hook"] = make_hook(c)
        c["score"] = score_lead(c)
        c["owner_id"] = uid

    ts = now_db()
    with db.transaction(conn):
        key_to_id: dict[str, int] = {}
        for c in qualified:
            key_to_id[c["_key"]] = _insert_lead(conn, c, search_id, ts)
        for c in result.blocked:
            c["status"] = "dnc"
            c["blocked_reason"] = "Number is on the Do Not Call list"
            c["website_issues"] = []
            _insert_lead(conn, c, search_id, ts)
        for c, ref in result.duplicates:
            primary_id = ref if isinstance(ref, int) else key_to_id.get(ref)
            if primary_id is None:
                continue  # its primary didn't qualify; don't store
            c["duplicate_of"] = primary_id
            c["status"] = "duplicate"
            c["website_issues"] = []
            _insert_lead(conn, c, search_id, ts)
        for c, existing in result.known:
            sets = ", ".join(f"{f}=?" for f in GOOGLE_FIELDS)
            conn.execute(
                f"UPDATE leads SET {sets}, last_refreshed=? WHERE id=?",
                [c[f] for f in GOOGLE_FIELDS] + [ts, existing["id"]],
            )
        for c in qualified:
            c["id"] = key_to_id[c["_key"]]

        funnel = [
            ("Raw results", len(all_raw)),
            ("Open and in the area", after_area),
            ("Right category", len(pool)),
            ("Not a chain", result.counts.get("chains", 0)),
            ("Duplicates merged", result.counts.get("duplicates", 0)),
            ("Not already handled", result.counts.get("handled", 0)),
            ("Callable phone", result.counts.get("phone", 0)),
        ]
        website = Counter(c.get("website_status") or "unchecked" for c in qualified)
        cost = conn.execute(
            "SELECT COALESCE(SUM(est_cost_gbp),0) FROM api_usage WHERE search_id=?", (search_id,)
        ).fetchone()[0]
        conn.execute(
            """UPDATE searches SET queries_json=?, raw_results=?, qualified=?, already_known=?, requests_made=?,
               est_cost_gbp=?, funnel_json=?, stop_reason=?, status='done', finished_at=? WHERE id=?""",
            (
                json.dumps(report.queries), len(all_raw), len(qualified), len(result.known), requests_made,
                float(cost), json.dumps({"funnel": funnel, "handled": dict(result.handled), "website": dict(website),
                                         "closed": closed, "message": stop_message}),
                stop_reason, now_db(), search_id,
            ),
        )
        conn.execute(
            "INSERT INTO activity(user_id, lead_id, kind, summary, tag, created_at) VALUES (?,?,?,?,?,?)",
            (uid, None, "search", f"Searched {trade.label.lower()} in {town}: {len(qualified)} new leads", "Search", now_db()),
        )

    report.funnel = funnel
    report.handled = dict(result.handled)
    report.closed_skipped = closed
    report.website = dict(website)
    report.requests_made = requests_made
    report.est_cost_gbp = float(cost)
    report.stop_reason = stop_reason
    report.stop_message = stop_message
    report.new_leads = qualified
    report.known_count = len(result.known)
    report.raw_count = len(all_raw)
    return report


LEAD_COLUMNS = (
    "place_id", "name", "trade", "town", "address", "postcode", "phone", "phone_e164", "phone_type", "website",
    "rating", "review_count", "business_status", "primary_type", "maps_url", "lat", "lng", "status",
    "website_status", "website_issues", "audit_json", "pagespeed_score", "audited_at", "existing_agency",
    "ai_dated_score", "ai_verdict", "ai_biggest_problem", "hook", "quality_flags", "entity_type", "entity_source",
    "company_number", "email", "email_source", "score", "owner_id", "duplicate_of", "blocked_reason", "search_id",
    "country",
)


def _insert_lead(conn: sqlite3.Connection, c: dict[str, Any], search_id: int, ts: str) -> int:
    row = dict(c)
    row.setdefault("status", "new")
    row["search_id"] = search_id
    row["website_issues"] = json.dumps(row.get("website_issues") or [])
    row["quality_flags"] = json.dumps(row.get("quality_flags") or [])
    row.setdefault("entity_type", "unknown")
    row.setdefault("score", score_lead(c))
    cols = [k for k in LEAD_COLUMNS if k in row]
    cur = conn.execute(
        f"INSERT INTO leads({', '.join(cols)}, first_seen, last_refreshed) VALUES ({', '.join('?' * len(cols))}, ?, ?) "
        f"ON CONFLICT(place_id) DO UPDATE SET last_refreshed=excluded.last_refreshed",
        [row[k] for k in cols] + [ts, ts],
    )
    if cur.lastrowid:
        got = conn.execute("SELECT id FROM leads WHERE place_id=?", (c["place_id"],)).fetchone()
        return int(got["id"])
    return int(conn.execute("SELECT id FROM leads WHERE place_id=?", (c["place_id"],)).fetchone()["id"])


def coverage(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Trades x towns: lead counts, dials, meetings and the last search date."""
    rows = conn.execute(
        """
        SELECT l.trade, l.town, COUNT(DISTINCT l.id) AS leads,
               (SELECT COUNT(*) FROM calls c JOIN leads l2 ON l2.id=c.lead_id WHERE l2.trade=l.trade AND l2.town=l.town) AS dials,
               (SELECT COUNT(*) FROM meetings m JOIN leads l3 ON l3.id=m.lead_id WHERE l3.trade=l.trade AND l3.town=l.town) AS meetings,
               (SELECT MAX(created_at) FROM searches s WHERE s.trade=l.trade AND s.town=l.town) AS last_search
        FROM leads l WHERE l.duplicate_of IS NULL AND l.status != 'dnc'
        GROUP BY l.trade, l.town ORDER BY l.town, l.trade
        """
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Refresh (Google's terms limit how long Places content may be cached)
# ---------------------------------------------------------------------------

@dataclass
class RefreshReport:
    refreshed: int = 0
    closed: int = 0
    failed: int = 0
    stopped: str = ""
    candidates: int = 0


def refresh_leads(
    conn: sqlite3.Connection,
    settings: Settings,
    clients: Clients,
    user: User | None,
    older_than_days: int = 30,
    limit: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> RefreshReport:
    """Re-fetch Place Details for active leads not refreshed recently, oldest first,
    stopping at the free-tier limit."""
    from .clients import DETAILS_FIELD_MASK
    from .freetier import PLACE_DETAILS

    guard = UsageGuard(conn, settings)
    cutoff = to_db(now_utc() - timedelta(days=older_than_days))
    sql = """SELECT * FROM leads WHERE last_refreshed < ? AND duplicate_of IS NULL
             AND status NOT IN ('lost','dnc','won','duplicate') ORDER BY last_refreshed ASC"""
    rows = [dict(r) for r in conn.execute(sql, (cutoff,)).fetchall()]
    if limit:
        rows = rows[:limit]
    rep = RefreshReport(candidates=len(rows))
    uid = user.id if user else None
    for i, lead in enumerate(rows):
        try:
            guard.check(PLACE_DETAILS)
        except AllowanceReached as e:
            rep.stopped = str(e)
            break
        try:
            place = clients.places.place_details(lead["place_id"], DETAILS_FIELD_MASK)
        except ExternalError:
            rep.failed += 1
            continue
        guard.record(PLACE_DETAILS, uid, None)
        fresh = place_to_lead(place, Trade(key=lead["trade"] or "", label=lead["trade"] or ""), lead["town"] or "",
                              lead.get("country") or "GB")
        merged = {**lead, **{k: fresh[k] for k in GOOGLE_FIELDS}}
        merged["hook"] = make_hook(merged)
        merged["score"] = score_lead(merged)
        sets = ", ".join(f"{f}=?" for f in GOOGLE_FIELDS)
        conn.execute(
            f"UPDATE leads SET {sets}, hook=?, score=?, last_refreshed=? WHERE id=?",
            [fresh[f] for f in GOOGLE_FIELDS] + [merged["hook"], merged["score"], now_db(), lead["id"]],
        )
        if fresh["business_status"] != "OPERATIONAL":
            conn.execute(
                "UPDATE leads SET status='lost', lost_reason='Closed on Google', stage_changed_at=? WHERE id=?",
                (now_db(), lead["id"]),
            )
            rep.closed += 1
        db.commit(conn)
        rep.refreshed += 1
        if progress:
            progress(i + 1, len(rows))
    return rep
