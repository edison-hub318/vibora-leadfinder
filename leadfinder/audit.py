"""Website audit: judge each lead's website and turn the problems into a pitch hook.

Free checks always run (domain lists, our own homepage fetch, PageSpeed Insights, up to 10
link checks). The optional AI judgement runs only when ANTHROPIC_API_KEY is set.
"""
from __future__ import annotations

import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from . import db
from .clients import Clients, FetchResult, json_loads_safe
from .config import Settings, load_website_rules
from .scoring import score_lead
from .timeutil import now_db, now_london

STATUSES = ("none", "social_only", "free_builder", "poor", "ok", "good")

PLATFORM_LABELS = {
    "facebook.com": "Facebook",
    "fb.com": "Facebook",
    "fb.me": "Facebook",
    "instagram.com": "Instagram",
    "linktr.ee": "Linktree",
    "x.com": "X (Twitter)",
    "twitter.com": "X (Twitter)",
    "tiktok.com": "TikTok",
    "yell.com": "Yell",
    "checkatrade.com": "Checkatrade",
    "bark.com": "Bark",
    "business.site": "Google Business",
    "g.page": "Google Business",
    "business.google.com": "Google Business",
    "wixsite.com": "Wix",
    "wix.com": "Wix",
    "square.site": "Square",
    "squarespace.com": "Squarespace",
    "weebly.com": "Weebly",
    "weeblysite.com": "Weebly",
    "wordpress.com": "WordPress.com",
    "godaddysites.com": "GoDaddy",
    "jimdo.com": "Jimdo",
    "jimdofree.com": "Jimdo",
    "jimdosite.com": "Jimdo",
    "site123.me": "Site123",
    "blogspot.com": "Blogger",
}


def host_of(url: str | None) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _matches_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def classify_domain(url: str | None, rules: dict[str, Any]) -> tuple[str | None, str]:
    """('social_only' | 'free_builder' | None, platform label)."""
    host = host_of(url)
    if not host:
        return None, ""
    for d in rules.get("social_domains", []):
        if _matches_domain(host, d):
            return "social_only", PLATFORM_LABELS.get(d, d)
    for d in rules.get("free_builder_domains", []):
        if _matches_domain(host, d):
            return "free_builder", PLATFORM_LABELS.get(d, d)
    return None, ""


def shareable_domain(url: str | None, rules: dict[str, Any]) -> str:
    """Domain used for the 'shared by 3+ listings' chain check ('' for social/builder hosts)."""
    host = host_of(url)
    kind, _ = classify_domain(url, rules)
    return "" if (not host or kind) else host


# ---------------------------------------------------------------------------
# HTML signals
# ---------------------------------------------------------------------------

class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta_description = ""
        self.viewport = False
        self.links: list[tuple[str, str]] = []  # (href, anchor text)
        self.tel_links = 0
        self.mailto: list[str] = []
        self.forms = 0
        self.contact_form = False
        self.tag_counts: dict[str, int] = {}
        self.text_parts: list[str] = []
        self._in_title = False
        self._skip = 0
        self._in_form = False
        self._form_has_input = False
        self._current_a: list[str] | None = None
        self._current_href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        self.tag_counts[tag] = self.tag_counts.get(tag, 0) + 1
        if tag in ("script", "style", "noscript", "template"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = a.get("name", "").lower()
            if name == "viewport":
                self.viewport = True
            elif name == "description" and a.get("content", "").strip():
                self.meta_description = a["content"].strip()
        elif tag == "a":
            href = a.get("href", "").strip()
            if href.lower().startswith("tel:"):
                self.tel_links += 1
            elif href.lower().startswith("mailto:"):
                self.mailto.append(href[7:].split("?")[0])
            self._current_a = []
            self._current_href = href
        elif tag == "form":
            self.forms += 1
            self._in_form = True
            self._form_has_input = False
        elif tag in ("input", "textarea") and self._in_form:
            typ = a.get("type", "text").lower()
            nm = (a.get("name", "") + a.get("id", "") + a.get("placeholder", "")).lower()
            if tag == "textarea" or typ == "email" or "email" in nm or "message" in nm:
                self._form_has_input = True
        elif tag == "embed" or tag == "object":
            src = (a.get("src", "") + a.get("data", "") + a.get("type", "")).lower()
            if "swf" in src or "flash" in src:
                self.tag_counts["flash"] = self.tag_counts.get("flash", 0) + 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "template") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._current_a is not None:
            self.links.append((self._current_href, " ".join(self._current_a).strip()))
            self._current_a = None
        elif tag == "form":
            if self._form_has_input:
                self.contact_form = True
            self._in_form = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        t = data.strip()
        if t:
            self.text_parts.append(t)
            if self._current_a is not None:
                self._current_a.append(t)

    @property
    def text(self) -> str:
        return "\n".join(self.text_parts)


def parse_page(html: str) -> _PageParser:
    p = _PageParser()
    try:
        p.feed(html)
        p.close()
    except Exception:  # malformed HTML: keep whatever we got
        pass
    return p


UK_PHONE_RE = re.compile(r"(?:\+44\s?\(?0?\)?\s?|\b0)(?:\d[\s\-]?){9,10}\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def latest_copyright_year(text: str, current_year: int) -> int | None:
    years: list[int] = []
    for m in re.finditer(r"(?:©|&copy;|\(c\)|copyright)([^\n]{0,60})", text, flags=re.I):
        for y in re.findall(r"(?<!\d)(19\d\d|20\d\d)(?!\d)", m.group(1)):
            yi = int(y)
            if 1995 <= yi <= current_year + 1:
                years.append(yi)
    return max(years) if years else None


AGENCY_RE = re.compile(
    r"(?:web\s*site|site|web\s*design|designed|design|built|developed|created|made|powered)"
    r"(?:\s+and\s+\w+)?\s+by\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9&.'\- ]{1,40})",
    re.I,
)


def find_agency(text: str, platform_names: list[str]) -> str | None:
    tail = text[-4000:]
    for m in AGENCY_RE.finditer(tail):
        name = re.split(r"\s*(?:\||©|·|•|\n|,|  |\s-\s|\bcopyright\b|\ball rights\b)", m.group(1), flags=re.I)[0]
        name = name.strip(" .-'")
        if len(name) < 2:
            continue
        low = name.lower()
        if any(low.startswith(p) for p in platform_names):
            continue
        if low in {"us", "me", "the owner", "our team", "hand", "local people"}:
            continue
        return name[:40]
    return None


def is_parked(page: _PageParser, html: str, rules: dict[str, Any]) -> bool:
    low = (page.text + " " + page.title).lower()
    if any(p in low or p in html.lower()[:20000] for p in rules.get("parked_phrases", [])):
        return True
    if len(page.text) < 600 and any(p in low for p in rules.get("placeholder_phrases", [])):
        return True
    return False


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------

@dataclass
class AuditResult:
    status: str
    issues: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
    pagespeed: int | None = None
    existing_agency: str | None = None
    ai_dated_score: int | None = None
    ai_verdict: str | None = None
    ai_biggest_problem: str | None = None
    page_text: str = ""

    def as_update(self) -> dict[str, Any]:
        return {
            "website_status": self.status,
            "website_issues": json.dumps(self.issues),
            "audit_json": json.dumps(self.signals),
            "pagespeed_score": self.pagespeed,
            "existing_agency": self.existing_agency,
            "ai_dated_score": self.ai_dated_score,
            "ai_verdict": self.ai_verdict,
            "ai_biggest_problem": self.ai_biggest_problem,
            "audited_at": now_db(),
        }


class _Robots:
    """robots.txt cache for one audit (we only touch a handful of URLs per site)."""

    def __init__(self, fetcher: Any):
        self.fetcher = fetcher
        self.cache: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self.cache:
            res = self.fetcher.get(base + "/robots.txt", timeout=8)
            if res.ok and res.status == 200 and res.text and "<html" not in res.text[:200].lower():
                rp = RobotFileParser()
                rp.parse(res.text.splitlines())
                self.cache[base] = rp
            else:
                self.cache[base] = None  # no robots.txt = allowed
        rp = self.cache[base]
        return True if rp is None else rp.can_fetch("*", url)


def _normalise_url(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = "http://" + url
    return url


def audit_website(url: str | None, clients: Clients, rules: dict[str, Any]) -> AuditResult:
    """Run the free checks on one website."""
    if not url:
        return AuditResult(status="none", issues=["No website"])
    kind, platform = classify_domain(url, rules)
    if kind == "social_only":
        return AuditResult(status="social_only", issues=[f"{platform} page only, no real website"], signals={"platform": platform})
    if kind == "free_builder":
        return AuditResult(
            status="free_builder",
            issues=[f"Free {platform} site ({host_of(url)})"],
            signals={"platform": platform},
        )

    th = rules.get("thresholds", {})
    url = _normalise_url(url)
    robots = _Robots(clients.fetcher)
    major: list[str] = []
    minor: list[str] = []
    sig: dict[str, Any] = {"url": url}
    this_year = now_london().year

    if not robots.allowed(url):
        sig["robots_blocked"] = True
        res = FetchResult(url=url, error="robots")
    else:
        res = clients.fetcher.get(url, timeout=float(th.get("fetch_timeout", 15)))
        # A plain-http URL that fails might work over https (and vice versa) - try once.
        if not res.ok and res.error != "ssl" and url.startswith("http://"):
            alt = clients.fetcher.get("https://" + url[7:], timeout=float(th.get("fetch_timeout", 15)))
            if alt.ok:
                res = alt
    sig["loaded"] = res.ok
    sig["status"] = res.status
    sig["final_url"] = res.final_url
    sig["error"] = res.error

    page = None
    if res.error == "robots":
        pass
    elif res.error == "ssl":
        sig["valid_certificate"] = False
        major.append("Security certificate error (browsers show a warning)")
    elif not res.ok:
        major.append("Website times out" if res.error == "timeout" else "Website doesn't load")
    else:
        final = res.final_url or url
        sig["https"] = final.startswith("https://")
        sig["valid_certificate"] = True if sig["https"] else None
        if not sig["https"]:
            major.append("No HTTPS (browsers say 'Not secure')")
        html = res.text
        page = parse_page(html)
        sig.update(
            title=bool(page.title.strip()),
            meta_description=bool(page.meta_description),
            viewport=page.viewport,
            phone_visible=bool(page.tel_links or UK_PHONE_RE.search(page.text)),
            contact_form=page.contact_form,
        )
        contact_links = [h for h, t in page.links if "contact" in (h + " " + t).lower()]
        sig["contact_link"] = bool(contact_links)

        if not page.viewport:
            major.append("Site not mobile-friendly")
        parked = is_parked(page, html, rules)
        if host_of(final) and host_of(final) != host_of(url) and parked:
            parked = True
        sig["parked"] = parked
        if parked:
            major.append("Domain parked or for sale")

        year = latest_copyright_year(page.text, this_year)
        sig["copyright_year"] = year
        if year:
            age = this_year - year
            if age > int(th.get("copyright_poor_years", 3)):
                major.append(f"Copyright {year}")
            elif age > int(th.get("copyright_stale_years", 2)):
                minor.append(f"Copyright {year}")

        tc = page.tag_counts
        if tc.get("flash") or ".swf" in html.lower():
            major.append("Uses Flash")
            sig["flash"] = True
        if tc.get("frameset") or tc.get("frame"):
            major.append("Built with frames")
            sig["frames"] = True
        if tc.get("table", 0) >= 3 and tc.get("div", 0) < 10:
            minor.append("Old table-based layout")
            sig["table_layout"] = True
        if not page.title.strip():
            minor.append("No page title")
        if not page.meta_description:
            minor.append("No meta description")
        if not sig["phone_visible"]:
            minor.append("No phone number on the homepage")

        # Contact form: homepage, else the contact page (one extra fetch).
        has_form = page.contact_form
        if not has_form and contact_links:
            curl = urljoin(final, contact_links[0])
            if host_of(curl) == host_of(final) and robots.allowed(curl):
                cres = clients.fetcher.get(curl, timeout=float(th.get("fetch_timeout", 15)))
                if cres.ok:
                    has_form = parse_page(cres.text).contact_form
        sig["contact_form"] = has_form
        if not has_form:
            minor.append("No contact form")

        agency = find_agency(page.text, [p.lower() for p in rules.get("platform_names", [])])

        # Broken internal links (up to N).
        broken = 0
        checked = 0
        seen: set[str] = set()
        for href, _ in page.links:
            if checked >= int(th.get("max_links_checked", 10)):
                break
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
                continue
            absu = urljoin(final, href).split("#")[0]
            if host_of(absu) != host_of(final) or absu in seen or absu.rstrip("/") == final.rstrip("/"):
                continue
            seen.add(absu)
            if not robots.allowed(absu):
                continue
            checked += 1
            lr = clients.fetcher.head(absu, timeout=10)
            if lr.error or lr.status >= 400:
                broken += 1
        sig["links_checked"] = checked
        sig["broken_links"] = broken
        if broken >= int(th.get("broken_links_poor", 3)):
            major.append(f"{broken} broken links")
        elif broken:
            minor.append(f"{broken} broken link{'s' if broken > 1 else ''}")

    # PageSpeed (Google fetches the page itself, so robots doesn't block this).
    ps = clients.pagespeed.mobile_score(url) if (res.ok or res.error == "robots") else None
    sig["pagespeed"] = ps
    if ps is not None:
        if ps < int(th.get("pagespeed_poor_below", 50)):
            major.append(f"Slow on mobile (PageSpeed {ps}/100)")
        elif ps < int(th.get("pagespeed_minor_below", 70)):
            minor.append(f"A bit slow on mobile (PageSpeed {ps}/100)")

    if major or len(minor) > int(th.get("ok_max_minor_issues", 2)):
        status = "poor"
    elif minor:
        status = "ok"
    elif res.error == "robots" and ps is None:
        status = "ok"  # couldn't check; don't claim it's good
    else:
        status = "good"

    return AuditResult(
        status=status,
        issues=major + minor,
        signals=sig,
        pagespeed=ps,
        existing_agency=agency if page is not None else None,
        page_text=page.text[:6000] if page is not None else "",
    )


# ---------------------------------------------------------------------------
# Optional AI judgement
# ---------------------------------------------------------------------------

AI_PROMPT = """You are judging a small UK business's website for a web design agency deciding whether to pitch a redesign.
Business: {name} ({trade}, {town}).
Website: {url}
Mobile PageSpeed score: {pagespeed}
Signals from our checks: {signals}
Issues found: {issues}

Homepage text (truncated):
\"\"\"
{text}
\"\"\"

Return JSON only with:
- dated_score: integer 1-10, how dated or weak the site looks (10 = very dated, a clear redesign case)
- verdict: one sentence the caller can use as the reason to pitch, specific to this business
- biggest_problem: a short phrase naming the single biggest problem"""


def validate_ai(text: str | None) -> dict[str, Any] | None:
    data = json_loads_safe(text)
    if not isinstance(data, dict):
        return None
    try:
        score = int(data["dated_score"])
        verdict = str(data["verdict"]).strip()
        problem = str(data["biggest_problem"]).strip()
    except (KeyError, TypeError, ValueError):
        return None
    if not (1 <= score <= 10) or not verdict or len(verdict) > 400 or len(problem) > 200:
        return None
    return {"dated_score": score, "verdict": verdict, "biggest_problem": problem}


def ai_judge(lead: dict[str, Any], result: AuditResult, clients: Clients) -> None:
    """Upgrade the result in place from the AI's verdict. Fails quietly."""
    if clients.ai is None or result.status not in ("ok", "poor"):
        return
    prompt = AI_PROMPT.format(
        name=lead.get("name", ""),
        trade=lead.get("trade", ""),
        town=lead.get("town", ""),
        url=lead.get("website", ""),
        pagespeed=result.pagespeed if result.pagespeed is not None else "unknown",
        signals=json.dumps({k: v for k, v in result.signals.items() if k not in ("url", "final_url")}),
        issues="; ".join(result.issues) or "none",
        text=result.page_text[:4000],
    )
    try:
        parsed = validate_ai(clients.ai.judge(prompt))
    except Exception:
        parsed = None
    if not parsed:
        return
    result.ai_dated_score = parsed["dated_score"]
    result.ai_verdict = parsed["verdict"]
    result.ai_biggest_problem = parsed["biggest_problem"]
    if parsed["dated_score"] >= 7 and result.status == "ok":
        result.status = "poor"


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------

def reviews_phrase(lead: dict[str, Any]) -> str:
    n = int(lead.get("review_count") or 0)
    r = lead.get("rating")
    if n and r:
        return f"{n} review{'s' if n != 1 else ''} at {float(r):.1f} stars"
    if n:
        return f"{n} review{'s' if n != 1 else ''}"
    return ""


def _lc_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s and not s[:2].isupper() else s


def make_hook(lead: dict[str, Any]) -> str:
    """Short, specific line for the call/email, e.g. '142 reviews at 4.8 stars but no website'."""
    status = lead.get("website_status") or ("none" if not lead.get("website") else None)
    issues = lead.get("website_issues") or []
    if isinstance(issues, str):
        issues = json_loads_safe(issues) or []
    rp = reviews_phrase(lead)
    if status == "none":
        return f"{rp} but no website" if rp else "No website at all"
    if status == "social_only":
        first = issues[0].split(",")[0] if issues else "Social page only"
        first = first.replace(" page only", " page only") if "page only" in first else first
        return f"{first}, {rp}" if rp else first
    if status == "free_builder":
        first = issues[0] if issues else "Free website builder"
        return f"{first}, {rp}" if rp else first
    if status in ("poor", "ok") and issues:
        parts = [issues[0]] + [_lc_first(i) for i in issues[1:3]]
        return ", ".join(parts)
    if status == "good":
        return f"Decent website; {rp}" if rp else "Decent website already"
    return f"Website not checked yet; {rp}" if rp else "Website not checked yet"


# ---------------------------------------------------------------------------
# Batch runner (used by search and by `leadfinder audit`)
# ---------------------------------------------------------------------------

def audit_many(
    leads: list[dict[str, Any]],
    clients: Clients,
    settings: Settings,
    ai_budget: int | None = None,
    progress: Callable[[int, int], None] | None = None,
    workers: int = 5,
) -> dict[Any, AuditResult]:
    """Audit leads concurrently (at most `workers` at a time). Keyed by lead['_key'] or lead['id']."""
    rules = load_website_rules(settings)
    results: dict[Any, AuditResult] = {}
    total = len(leads)
    if progress:
        progress(0, total)

    def work(lead: dict[str, Any]) -> tuple[Any, AuditResult]:
        try:
            r = audit_website(lead.get("website"), clients, rules)
        except Exception:
            r = AuditResult(status="ok" if lead.get("website") else "none", issues=[], signals={"error": "audit_failed"})
        return lead.get("_key", lead.get("id")), r

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(work, lead) for lead in leads]
        for f in as_completed(futures):
            key, r = f.result()
            results[key] = r
            done += 1
            if progress:
                progress(done, total)

    # AI judgement afterwards, capped per run.
    if clients.ai is not None:
        budget = settings.ai_max_per_search if ai_budget is None else ai_budget
        by_key = {lead.get("_key", lead.get("id")): lead for lead in leads}
        for key, r in results.items():
            if budget <= 0:
                break
            if r.status in ("ok", "poor"):
                ai_judge(by_key[key], r, clients)
                budget -= 1
    return results


def ai_calls_estimate(leads: list[dict[str, Any]], settings: Settings, rules: dict[str, Any]) -> int:
    """Upper bound on AI calls: leads with a real website (not social/builder), capped."""
    n = sum(1 for lead in leads if lead.get("website") and classify_domain(lead["website"], rules)[0] is None)
    return min(n, settings.ai_max_per_search)


def apply_result(lead: dict[str, Any], r: AuditResult) -> dict[str, Any]:
    lead.update(r.as_update())
    lead["website_issues"] = r.issues
    lead["hook"] = make_hook(lead)
    lead["score"] = score_lead(lead)
    return lead


def audit_command(
    conn: sqlite3.Connection,
    settings: Settings,
    clients: Clients,
    limit: int = 50,
    recheck: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """`leadfinder audit`: audit older leads that haven't been audited (or all, with recheck)."""
    sql = "SELECT * FROM leads WHERE duplicate_of IS NULL"
    if not recheck:
        sql += " AND audited_at IS NULL"
    sql += " ORDER BY score DESC, id LIMIT ?"
    leads = [db.row_to_dict(r) for r in conn.execute(sql, (limit,)).fetchall()]
    results = audit_many(leads, clients, settings, progress=progress)  # type: ignore[arg-type]
    counts: dict[str, int] = {s: 0 for s in STATUSES}
    with db.transaction(conn):
        for lead in leads:
            r = results[lead["id"]]
            apply_result(lead, r)
            counts[r.status] = counts.get(r.status, 0) + 1
            conn.execute(
                """UPDATE leads SET website_status=?, website_issues=?, audit_json=?, pagespeed_score=?,
                   existing_agency=?, ai_dated_score=?, ai_verdict=?, ai_biggest_problem=?, audited_at=?,
                   hook=?, score=? WHERE id=?""",
                (
                    lead["website_status"], json.dumps(lead["website_issues"]), lead["audit_json"],
                    lead["pagespeed_score"], lead["existing_agency"], lead["ai_dated_score"],
                    lead["ai_verdict"], lead["ai_biggest_problem"], lead["audited_at"],
                    lead["hook"], lead["score"], lead["id"],
                ),
            )
    return counts
