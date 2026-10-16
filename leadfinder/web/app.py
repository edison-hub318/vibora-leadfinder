"""The local dashboard (FastAPI + Jinja2 + HTMX). It calls the same functions as the CLI."""
from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import threading
from datetime import timedelta
from typing import Any, Callable, Iterator
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from markupsafe import Markup, escape

from .. import (backup as backup_mod, calls as calls_mod, db, emailer, enrich,
                leads as leads_mod, meetings as meetings_mod, phones, preview as preview_mod,
                search as search_mod, stats as stats_mod, users)
from ..clients import Clients
from ..config import STATIC_DIR, UI_TEMPLATES_DIR, Settings, load_towns
from ..freetier import UsageGuard, usage_by_search, usage_by_user
from ..leads import LeadError
from ..timeutil import fmt_local, from_db, humanize_ago, now_utc, parse_local, to_db, today_london
from ..trades import load_trades, resolve_trade, save_new_trade

COOKIE = "lf_session"
SHORT_SESSION = timedelta(hours=12)
LONG_SESSION = timedelta(days=30)
UNSAFE = ("POST", "PUT", "PATCH", "DELETE")

NAV = [
    ("Workspace", [("dashboard", "Dashboard", "/", "grid"), ("dial", "Dial", "/dial", "phone"),
                   ("leads", "Leads", "/leads", "list"), ("pipeline", "Pipeline", "/pipeline", "columns"),
                   ("meetings", "Meetings", "/meetings", "calendar")]),
    ("Outreach & tools", [("outreach", "Outreach", "/outreach", "mail"), ("previews", "Preview Sites", "/previews", "layout"),
                          ("objections", "Objections", "/objections", "message"), ("dnc", "Do Not Call", "/dnc", "slash")]),
    ("System", [("backups", "Backups", "/backups", "archive"), ("settings", "Settings", "/settings", "settings")]),
]

STATUS_LABELS = {
    "new": "New", "called": "Called", "callback": "Callback", "interested": "Interested",
    "meeting_booked": "Meeting booked", "proposal_sent": "Proposal sent", "won": "Won", "lost": "Lost",
    "not_interested": "Not interested", "wrong_number": "Wrong number", "dnc": "Do not call", "duplicate": "Duplicate",
}
WEBSITE_LABELS = {"none": "No website", "social_only": "Social only", "free_builder": "Free builder", "poor": "Poor",
                  "ok": "OK", "good": "Good", None: "Unchecked"}


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def render_script(text: str, lead: dict[str, Any] | None = None, my_name: str = "") -> Markup:
    """Tiny markdown subset for the call script: '## heading', '- bullet', paragraphs; fills placeholders."""
    values = {"my_name": my_name or "me"}
    if lead:
        values.update(name=lead.get("name", ""), trade=(lead.get("trade") or "").replace("_", " "), town=lead.get("town") or "")
    out = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        for k, v in values.items():
            line = line.replace("{" + k + "}", v)
        safe = str(escape(line.strip()))
        if not safe:
            continue
        if safe.startswith("#"):
            out.append(f"<h3>{safe.lstrip('#').strip()}</h3>")
        else:
            out.append(f"<p>{safe}</p>")
    return Markup("\n".join(out))


class SessionCodec:
    """Signed cookie: {'uid', 'csrf', 'remember', 'iat'}. Expiry is checked against our own clock."""

    def __init__(self, secret: str):
        self.s = URLSafeSerializer(secret, salt="leadfinder-session")

    def load(self, raw: str | None) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            data = self.s.loads(raw)
        except BadSignature:
            return {}
        if not isinstance(data, dict):
            return {}
        if data.get("uid"):
            iat = from_db(data.get("iat"))
            life = LONG_SESSION if data.get("remember") else SHORT_SESSION
            if iat is None or now_utc() - iat > life:
                return {"csrf": data.get("csrf")}
        return data

    def dump(self, data: dict[str, Any]) -> str:
        return self.s.dumps(data)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(settings: Settings, clients_factory: Callable[[Settings], Clients] | None = None) -> FastAPI:
    # Create the schema once; requests then open light connections.
    db.connect(settings.db_path).close()
    codec = SessionCodec(settings.secret_key)
    templates = Jinja2Templates(directory=str(UI_TEMPLATES_DIR))
    factory = clients_factory or Clients.real
    state: dict[str, Any] = {"clients": None, "jobs": {}, "backup_day": None, "skips": {}}
    lock = threading.Lock()

    def clients() -> Clients:
        with lock:
            if state["clients"] is None:
                state["clients"] = factory(settings)
            return state["clients"]

    def open_conn() -> sqlite3.Connection:
        return db.connect(settings.db_path, init=False)

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = open_conn()
        try:
            yield conn
        finally:
            conn.close()

    async def csrf_protect(request: Request) -> None:
        if request.method not in UNSAFE:
            return
        token = request.headers.get("X-CSRF-Token")
        if not token:
            ctype = request.headers.get("content-type", "")
            if "form" in ctype:
                form = await request.form()
                token = form.get("csrf_token")  # type: ignore[assignment]
        expected = request.state.session.get("csrf", "")
        if not token or not expected or not hmac.compare_digest(str(token), str(expected)):
            raise HTTPException(status_code=403, detail="CSRF check failed. Reload the page and try again.")

    app = FastAPI(title="Vibora Leadfinder", docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(csrf_protect)])
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ---- Jinja helpers ------------------------------------------------------
    env = templates.env
    env.filters["when"] = lambda v, t=True: fmt_local(v, t) if v else ""
    env.filters["ago"] = humanize_ago
    env.filters["phone"] = lambda v: phones.pretty(v) if v else ""
    env.filters["tel"] = lambda v: phones.classify(v).e164 if v else ""
    env.filters["money"] = lambda v: f"£{float(v or 0):,.0f}"
    env.filters["pct"] = lambda v: "-" if v is None else f"{v:.0f}%"
    env.filters["signed"] = lambda v, unit="": "" if v is None else (f"+{v:.0f}{unit}" if v > 0 else f"{v:.0f}{unit}")
    env.filters["status_label"] = lambda s: STATUS_LABELS.get(s, (s or "").replace("_", " ").capitalize())
    env.filters["web_label"] = lambda s: WEBSITE_LABELS.get(s, s)
    env.filters["trade_label"] = lambda s: (s or "").replace("_", " ").capitalize()
    env.filters["duration"] = lambda s: f"{int(s or 0) // 60}:{int(s or 0) % 60:02d}"
    env.filters["hours"] = lambda s: f"{(s or 0) / 3600:.1f}h"
    env.filters["tojson_attr"] = lambda v: json.dumps(v)
    env.filters["script"] = render_script
    env.globals["NAV"] = NAV
    env.globals["display_hook"] = leads_mod.display_hook

    # ---- middleware: session, login wall, daily backup ---------------------
    @app.middleware("http")
    async def session_mw(request: Request, call_next):  # type: ignore[no-untyped-def]
        sess = codec.load(request.cookies.get(COOKIE))
        dirty = False
        if not sess.get("csrf"):
            sess["csrf"] = secrets.token_urlsafe(24)
            dirty = True
        request.state.session = sess
        request.state.set_session = None
        request.state.user = None
        path = request.url.path
        public = path.startswith("/static/") or path in ("/login", "/favicon.ico")
        if sess.get("uid"):
            conn = open_conn()
            try:
                request.state.user = users.get_user(conn, int(sess["uid"]))
                today = today_london().isoformat()
                if state["backup_day"] != today:
                    backup_mod.ensure_daily_backup(conn, settings)
                    state["backup_day"] = today
            finally:
                conn.close()
        if request.state.user is None and not public:
            if request.headers.get("HX-Request"):
                response: Response = Response(status_code=401, headers={"HX-Redirect": "/login"})
            else:
                response = RedirectResponse("/login", status_code=303)
        else:
            response = await call_next(request)
        new = request.state.set_session
        if new is not None:
            sess, dirty = new, True
        if dirty:
            remember = bool(sess.get("remember"))
            response.set_cookie(
                COOKIE, codec.dump(sess), httponly=True, samesite="lax",
                secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
                max_age=int(LONG_SESSION.total_seconds()) if remember else None, path="/",
            )
        return response

    # ---- render helper ------------------------------------------------------
    def me(request: Request) -> users.User:
        return request.state.user

    def render(request: Request, conn: sqlite3.Connection, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
        u = request.state.user
        base: dict[str, Any] = {
            "request": request,
            "user": u,
            "csrf": request.state.session.get("csrf", ""),
            "settings": settings,
        }
        if u is not None and not ctx.get("partial"):
            scope = request.cookies.get("lf_recent", "me")
            base["recent_scope"] = scope
            base["recent"] = stats_mod.recent_items(conn, None if scope == "team" else [u.id])
            age = backup_mod.backup_age_hours(settings)
            base["backup_stale"] = age is None or age > 48
            base["all_users"] = users.list_users(conn)
        base.update(ctx)
        return templates.TemplateResponse(request, name, base, status_code=status_code)

    def toast(message: str, kind: str = "ok") -> dict[str, str]:
        return {"HX-Trigger": json.dumps({"toast": {"message": message, "kind": kind}})}

    def who_filter(request: Request, conn: sqlite3.Connection, who: str | None) -> tuple[str, list[int] | None]:
        u = me(request)
        who = who or str(u.id)
        if who == "team":
            return "team", None
        try:
            uid = int(who)
        except ValueError:
            uid = u.id
        return str(uid), [uid]

    # ======================================================================
    # Login
    # ======================================================================

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        if request.state.user:
            return RedirectResponse("/", status_code=303)
        return render(request, conn, "login.html", error=None, no_users=users.user_count(conn) == 0)

    @app.post("/login", response_class=HTMLResponse)
    def login_post(request: Request, full_name: str = Form(""), password: str = Form(""),
                   remember: str | None = Form(None), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        try:
            u = users.authenticate(conn, full_name, password)
        except users.AuthError as e:
            msg = str(e) if "Too many" in str(e) else "Name or password incorrect"
            return render(request, conn, "login.html", status_code=401, error=msg, full_name=full_name, no_users=False)
        request.state.set_session = {"uid": u.id, "csrf": secrets.token_urlsafe(24), "remember": bool(remember),
                                     "iat": to_db(now_utc())}
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request) -> Response:
        request.state.set_session = {"csrf": secrets.token_urlsafe(24)}
        resp = RedirectResponse("/login", status_code=303)
        return resp

    # ======================================================================
    # Dashboard
    # ======================================================================

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, period: str = "today", who: str | None = None,
                  conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        period = period if period in stats_mod.PERIODS else "today"
        who_key, ids = who_filter(request, conn, who)
        guard = UsageGuard(conn, settings)
        due = [lead for lead in leads_mod.queue(conn, me(request).id, limit=50) if lead["queue_rank"] == 0]
        return render(
            request, conn, "dashboard.html", active="dashboard", title="Dashboard", period=period,
            periods=stats_mod.PERIODS, who=who_key,
            k=stats_mod.kpis(conn, period, ids),
            heat=stats_mod.heatmap(conn, period, ids),
            outcomes=stats_mod.outcome_breakdown(conn, period, ids),
            by_trade=stats_mod.performance(conn, period, ids, "trade"),
            by_town=stats_mod.performance(conn, period, ids, "town"),
            funnel=stats_mod.funnel(conn, period, ids),
            feed=stats_mod.activity_feed(conn, ids),
            leaderboard=stats_mod.leaderboard(conn, period),
            today_strip=stats_mod.today_strip(conn),
            callbacks=due,
            meter=guard.meter(),
        )

    # ======================================================================
    # Dial
    # ======================================================================

    def dial_context(request: Request, conn: sqlite3.Connection, lead_id: int | None = None) -> dict[str, Any]:
        u = me(request)
        lead = None
        lock_warning = None
        if lead_id:
            lead = leads_mod.get_lead(conn, lead_id)
            if lead:
                lock_warning = leads_mod.lock_lead(conn, lead_id, u.id)
        else:
            skips = state["skips"].setdefault(u.id, {})
            now = now_utc()
            for k in [k for k, until in skips.items() if until < now]:
                del skips[k]
            pick = next((r for r in leads_mod.queue(conn, u.id, limit=len(skips) + 1) if r["id"] not in skips), None)
            lead = None
            if pick:
                leads_mod.lock_lead(conn, pick["id"], u.id)
                lead = leads_mod.get_lead(conn, pick["id"])
        ctx: dict[str, Any] = {"lead": lead, "lock_warning": lock_warning}
        if lead:
            blocked = None
            if leads_mod.is_dnc(conn, lead["phone"]):
                blocked = "This number is on the Do Not Call list."
            elif lead["tps_status"] == "registered":
                blocked = "TPS-registered: do not call."
            elif lead["status"] in ("won", "lost", "dnc", "wrong_number", "not_interested") and lead_id:
                blocked = f"This lead is {STATUS_LABELS.get(lead['status'], lead['status']).lower()}."
            ctx.update(
                blocked=blocked,
                history=leads_mod.call_history(conn, lead["id"]),
                tps_warning=leads_mod.tps_warning(lead),
                opener=leads_mod.call_opener(lead, settings.my_name or (u.full_name if u else "")),
            )
        return ctx

    def dial_common(conn: sqlite3.Connection) -> dict[str, Any]:
        tags = calls_mod.objection_tags(conn)
        notes = calls_mod.objection_notes(conn)
        script_path = settings.template_file("call_script.md")
        return {
            "outcomes": list(calls_mod.OUTCOMES.items()),
            "objection_tags": tags,
            "objection_outcomes": calls_mod.OBJECTION_OUTCOMES,
            "rebuttals": [(t["label"], notes.get(t["key"], "")) for t in tags if notes.get(t["key"])],
            "script": script_path.read_text(encoding="utf-8") if script_path.exists() else "",
        }

    @app.get("/dial", response_class=HTMLResponse)
    def dial(request: Request, lead: int | None = None, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        return render(request, conn, "dial.html", active="dial", title="Dial", **dial_context(request, conn, lead), **dial_common(conn))

    @app.get("/dial/next", response_class=HTMLResponse)
    def dial_next(request: Request, skip: int | None = None, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        if skip:
            # Skipped leads aren't served again to this person for 30 minutes.
            state["skips"].setdefault(me(request).id, {})[skip] = now_utc() + timedelta(minutes=30)
            leads_mod.unlock_lead(conn, skip, me(request).id)
        ctx = dial_context(request, conn)
        return render(request, conn, "partials/dial_card.html", partial=True, **ctx, **dial_common(conn))

    @app.post("/dial/log", response_class=HTMLResponse)
    def dial_log(
        request: Request,
        lead_id: int = Form(...), outcome: str = Form(...), note: str = Form(""),
        callback_at: str = Form(""), meeting_at: str = Form(""), duration: int = Form(0),
        session_id: str = Form(""), objection_other: str = Form(""), objections: list[str] = Form([]),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        tags = [t for t in objections if t]
        u = me(request)
        sid = int(session_id) if session_id.isdigit() else None
        try:
            res = calls_mod.log_call(
                conn, settings, lead_id, u.id, outcome, note=note or None,
                callback_at=parse_local(callback_at) if callback_at else None,
                meeting_at=parse_local(meeting_at) if meeting_at else None,
                duration_seconds=duration, objections=tags, objection_other=objection_other or None, session_id=sid,
            )
        except (LeadError, ValueError) as e:
            return HTMLResponse(f'<div class="error-box">{e}</div>', status_code=422, headers=toast(str(e), "error"))
        headers: dict[str, str] = {}
        msg = f"{calls_mod.OUTCOMES[outcome]} logged for {res['lead']['name']}"
        payload: dict[str, Any] = {"message": msg, "kind": "ok"}
        if res["meeting_id"]:
            m = meetings_mod.get_meeting(conn, res["meeting_id"])
            payload["calendar"] = {"ics": f"/meetings/{m['id']}.ics", "google": meetings_mod.google_calendar_url(m)}
            payload["message"] = f"Meeting booked with {m['name']} for {fmt_local(m['start_at'])}"
        headers["HX-Trigger"] = json.dumps({"toast": payload, "dialLogged": {"outcome": outcome}})
        ctx = dial_context(request, conn)
        resp = render(request, conn, "partials/dial_card.html", partial=True, **ctx, **dial_common(conn))
        resp.headers.update(headers)
        return resp

    @app.post("/dial/dnc", response_class=HTMLResponse)
    def dial_dnc(request: Request, lead_id: int = Form(...), reason: str = Form(""), do_not_email: str | None = Form(None),
                 duration: int = Form(0), session_id: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        u = me(request)
        sid = int(session_id) if session_id.isdigit() else None
        calls_mod.log_dnc_outcome(conn, lead_id, u.id, reason or None, bool(do_not_email), duration, sid)
        ctx = dial_context(request, conn)
        resp = render(request, conn, "partials/dial_card.html", partial=True, **ctx, **dial_common(conn))
        resp.headers.update(toast("Added to Do Not Call and closed"))
        resp.headers["HX-Trigger"] = json.dumps({"toast": {"message": "Added to Do Not Call and closed", "kind": "ok"},
                                                 "dialLogged": {"outcome": "dnc"}})
        return resp

    @app.post("/dial/session/start")
    def session_start(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        sid = calls_mod.start_session(conn, me(request).id)
        return Response(json.dumps({"id": sid}), media_type="application/json")

    @app.post("/dial/session/end")
    def session_end(request: Request, session_id: int = Form(...), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        calls_mod.end_session(conn, session_id, me(request).id)
        return Response(status_code=204)

    @app.post("/leads/{lead_id}/tps", response_class=HTMLResponse)
    def lead_tps(request: Request, lead_id: int, result: str = Form(...), back: str = Form("panel"),
                 conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        leads_mod.set_tps(conn, lead_id, result == "registered", me(request).id)
        if back == "dial":
            ctx = dial_context(request, conn) if result == "registered" else dial_context(request, conn, lead_id)
            return render(request, conn, "partials/dial_card.html", partial=True, **ctx, **dial_common(conn))
        return lead_panel(request, lead_id, conn)

    # ======================================================================
    # Leads + search
    # ======================================================================

    def lead_filters(request: Request) -> dict[str, Any]:
        q = request.query_params
        return {k: (q.get(k) or None) for k in ("q", "town", "trade", "status", "website", "owner", "sort")}

    def leads_rows(conn: sqlite3.Connection, f: dict[str, Any]) -> list[dict[str, Any]]:
        return leads_mod.list_leads(
            conn, town=f["town"], trade=f["trade"], status=f["status"], website_status=f["website"],
            owner_id=int(f["owner"]) if f.get("owner") else None, q=f["q"], sort=f["sort"] or "score", top=300,
        )

    def suggestions(conn: sqlite3.Connection) -> dict[str, Any]:
        trades = load_trades(settings)
        searched = [r[0] for r in conn.execute(
            "SELECT display_name FROM towns WHERE not_found=0 ORDER BY id DESC").fetchall()]
        starter = [t["name"] for t in load_towns(settings)]
        towns = list(dict.fromkeys(searched + starter))
        return {"trade_suggestions": [t.label for t in trades.values()], "town_suggestions": towns}

    @app.get("/leads", response_class=HTMLResponse)
    def leads_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        f = lead_filters(request)
        guard = UsageGuard(conn, settings)
        return render(
            request, conn, "leads.html", active="leads", title="Leads", f=f, rows=leads_rows(conn, f),
            towns=leads_mod.distinct_values(conn, "town"), trades=leads_mod.distinct_values(conn, "trade"),
            statuses=STATUS_LABELS, website_statuses=WEBSITE_LABELS, coverage=search_mod.coverage(conn),
            meter=guard.meter(), open_lead=request.query_params.get("open"), **suggestions(conn),
        )

    @app.get("/leads/table", response_class=HTMLResponse)
    def leads_table(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        f = lead_filters(request)
        return render(request, conn, "partials/leads_table.html", partial=True, rows=leads_rows(conn, f), f=f)

    @app.get("/leads/{lead_id}/panel", response_class=HTMLResponse)
    def lead_panel(request: Request, lead_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        lead = leads_mod.get_lead(conn, lead_id)
        if not lead:
            raise HTTPException(404)
        email_block = None
        if lead["entity_type"] != "limited_company":
            email_block = ("Sole trader: email not allowed, call instead" if "sole" in lead["entity_type"]
                           else "Company type unknown: email blocked until confirmed as a limited company")
        audit_signals = json.loads(lead["audit_json"]) if lead.get("audit_json") else {}
        return render(request, conn, "partials/lead_panel.html", partial=True, lead=lead,
                      history=leads_mod.call_history(conn, lead_id), timeline=leads_mod.lead_timeline(conn, lead_id),
                      email_block=email_block, signals=audit_signals, all_users=users.list_users(conn),
                      is_dnc=leads_mod.is_dnc(conn, lead["phone"]))

    @app.post("/leads/{lead_id}/assign", response_class=HTMLResponse)
    def lead_assign(request: Request, lead_id: int, owner_id: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        leads_mod.assign_owner(conn, lead_id, int(owner_id) if owner_id else None, me(request).id)
        resp = lead_panel(request, lead_id, conn)
        resp.headers.update(toast("Owner updated"))
        return resp

    @app.post("/leads/{lead_id}/email", response_class=HTMLResponse)
    def lead_email(request: Request, lead_id: int, email: str = Form(...), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        try:
            emailer.add_email(conn, lead_id, email, me(request).id)
        except emailer.EmailError as e:
            resp = lead_panel(request, lead_id, conn)
            resp.headers.update(toast(str(e), "error"))
            return resp
        resp = lead_panel(request, lead_id, conn)
        resp.headers.update(toast("Email saved"))
        return resp

    @app.post("/leads/{lead_id}/type", response_class=HTMLResponse)
    def lead_type(request: Request, lead_id: int, entity: str = Form(...), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        enrich.set_type(conn, lead_id, entity, me(request).id)
        resp = lead_panel(request, lead_id, conn)
        resp.headers.update(toast("Company type updated"))
        return resp

    @app.post("/leads/{lead_id}/stage", response_class=HTMLResponse)
    def lead_stage(request: Request, lead_id: int, stage: str = Form(...), setup: str = Form(""), retainer: str = Form(""),
                   reason: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        try:
            calls_mod.set_stage(conn, lead_id, me(request).id, stage, float(setup) if setup else None,
                                float(retainer) if retainer else None, reason or None)
        except (LeadError, ValueError) as e:
            return HTMLResponse(str(e), status_code=422, headers=toast(str(e), "error"))
        resp = lead_panel(request, lead_id, conn)
        resp.headers.update(toast(f"Moved to {STATUS_LABELS.get(stage, stage)}"))
        return resp

    # ---- New search (runs in a background thread; the form polls for progress) ----
    def run_job(job_id: str, user_id: int, opts: search_mod.SearchOptions) -> None:
        job = state["jobs"][job_id]
        conn = open_conn()
        try:
            user = users.get_user(conn, user_id)

            def on(event: str, data: dict[str, Any]) -> None:
                if event == "page":
                    job["stage"] = f"Searching: {data['query']} (page {data['page']}), {data['qualified']} qualified"
                    job["requests"] = data["requests"]
                elif event == "audit":
                    job["stage"] = "Auditing websites"
                    job["done"], job["total"] = data["done"], data["total"]
                elif event == "enrich":
                    job["stage"] = "Looking for emails"
                    job["done"], job["total"] = data["done"], data["total"]
                elif event == "plan":
                    job["planned"] = data["planned"]

            job["report"] = search_mod.run_search(conn, settings, clients(), user, opts, progress=on)
            job["status"] = "done"
        except search_mod.RepeatSearch as e:
            job["status"], job["error"], job["repeat"] = "repeat", str(e).replace(" Use --force to search again.", ""), True
        except search_mod.SearchError as e:
            job["status"], job["error"] = "error", str(e)
        except Exception as e:  # never leave the UI spinning
            job["status"], job["error"] = "error", f"Search failed: {type(e).__name__}"
        finally:
            conn.close()

    @app.post("/search", response_class=HTMLResponse)
    def start_search(request: Request, trade: str = Form(...), town: str = Form(...), max_leads: int = Form(30),
                     radius: float = Form(8.0), include_no_phone: str | None = Form(None), no_audit: str | None = Form(None),
                     force: str | None = Form(None), save_trade: str | None = Form(None),
                     conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        t = resolve_trade(settings, trade)
        if not t.known and save_trade:
            save_new_trade(settings, trade)
        opts = search_mod.SearchOptions(trade=trade, town=town, max_leads=max(1, min(200, max_leads)), radius_km=radius,
                                        include_no_phone=bool(include_no_phone), audit=not no_audit, force=bool(force))
        job_id = secrets.token_hex(8)
        guard = UsageGuard(conn, settings)
        state["jobs"][job_id] = {"status": "running", "stage": "Starting", "trade": trade, "town": town,
                                 "max_leads": opts.max_leads, "radius": radius,
                                 "unknown_trade": not t.known, "planned": search_mod.planned_requests(conn, guard, town, opts.max_leads)}
        threading.Thread(target=run_job, args=(job_id, me(request).id, opts), daemon=True).start()
        return render(request, conn, "partials/search_progress.html", partial=True, job_id=job_id, job=state["jobs"][job_id],
                      form={"trade": trade, "town": town, "max_leads": max_leads, "radius": radius,
                            "include_no_phone": include_no_phone, "no_audit": no_audit})

    @app.get("/search/job/{job_id}", response_class=HTMLResponse)
    def search_job(request: Request, job_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        job = state["jobs"].get(job_id)
        if not job:
            raise HTTPException(404)
        return render(request, conn, "partials/search_progress.html", partial=True, job_id=job_id, job=job,
                      form={"trade": job["trade"], "town": job["town"]})

    # ======================================================================
    # Pipeline
    # ======================================================================

    @app.get("/pipeline", response_class=HTMLResponse)
    def pipeline(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        cols = []
        for stage in calls_mod.STAGES:
            rows = [db.row_to_dict(r) for r in conn.execute(
                """SELECT l.*, u.initials AS owner_initials FROM leads l LEFT JOIN users u ON u.id=l.owner_id
                   WHERE l.status=? AND l.duplicate_of IS NULL ORDER BY l.stage_changed_at DESC LIMIT 100""", (stage,)
            ).fetchall()]
            cols.append({"stage": stage, "label": STATUS_LABELS[stage], "rows": rows})
        return render(request, conn, "pipeline.html", active="pipeline", title="Pipeline", cols=cols,
                      avg_deal=stats_mod.average_deal(conn))

    @app.post("/pipeline/move")
    def pipeline_move(request: Request, lead_id: int = Form(...), stage: str = Form(...), setup: str = Form(""),
                      retainer: str = Form(""), reason: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        try:
            calls_mod.set_stage(conn, lead_id, me(request).id, stage, float(setup) if setup else None,
                                float(retainer) if retainer else None, reason or None)
        except (LeadError, ValueError) as e:
            return Response(str(e), status_code=422, headers=toast(str(e), "error"))
        return Response(status_code=204, headers=toast(f"Moved to {STATUS_LABELS.get(stage, stage)}"))

    # ======================================================================
    # Meetings
    # ======================================================================

    @app.get("/meetings", response_class=HTMLResponse)
    def meetings_page(request: Request, who: str | None = "team", conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        who_key, ids = who_filter(request, conn, who)
        m = meetings_mod.list_meetings(conn, ids)
        days: dict[str, list[dict[str, Any]]] = {}
        for r in m["upcoming"]:
            r["gcal"] = meetings_mod.google_calendar_url(r)
            days.setdefault(fmt_local(r["start_at"], False), []).append(r)
        for r in m["awaiting"]:
            r["gcal"] = meetings_mod.google_calendar_url(r)
        return render(request, conn, "meetings.html", active="meetings", title="Meetings", days=days,
                      awaiting=m["awaiting"], who=who_key)

    @app.get("/meetings/{meeting_id}.ics")
    def meeting_ics(request: Request, meeting_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        try:
            m = meetings_mod.get_meeting(conn, meeting_id)
        except LeadError:
            raise HTTPException(404) from None
        fname = preview_mod.slugify(m["name"]) + "-meeting.ics"
        return Response(meetings_mod.build_ics(m), media_type="text/calendar; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @app.post("/meetings/{meeting_id}/outcome")
    def meeting_outcome(request: Request, meeting_id: int, outcome: str = Form(...), reason: str = Form(""),
                        conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        try:
            meetings_mod.meeting_outcome(conn, meeting_id, outcome, me(request).id, reason or None)
        except LeadError as e:
            raise HTTPException(422, str(e)) from None
        return RedirectResponse("/meetings", status_code=303)

    # ======================================================================
    # Outreach
    # ======================================================================

    def outreach_ctx(conn: sqlite3.Connection) -> dict[str, Any]:
        return {
            "pending": emailer.drafts(conn, "pending_review"),
            "approved": emailer.drafts(conn, "approved"),
            "sent": emailer.sent_emails(conn),
            "sent_today": emailer.sent_today(conn),
            "limit": emailer.daily_limit(conn, settings),
            "suppressed": emailer.suppression_list(conn),
            "smtp_ok": settings.smtp_configured,
        }

    @app.get("/outreach", response_class=HTMLResponse)
    def outreach(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        return render(request, conn, "outreach.html", active="outreach", title="Outreach", **outreach_ctx(conn))

    def outreach_reload(request: Request, conn: sqlite3.Connection, message: str, kind: str = "ok") -> Response:
        resp = render(request, conn, "partials/outreach_body.html", partial=True, **outreach_ctx(conn))
        resp.headers.update(toast(message, kind))
        return resp

    @app.post("/outreach/draft", response_class=HTMLResponse)
    def outreach_draft(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        rep = emailer.draft(conn, settings, me(request).id, 20)
        f = emailer.followup(conn, settings, me(request).id, 20)
        return outreach_reload(request, conn, f"Drafted {len(rep.created)} email(s) and {len(f.created)} follow-up(s)")

    @app.post("/outreach/{email_id}/{action}", response_class=HTMLResponse)
    def outreach_action(request: Request, email_id: int, action: str, subject: str = Form(""), body: str = Form(""),
                        conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        u = me(request)
        try:
            if action == "approve":
                emailer.approve(conn, email_id, u.id)
                msg = "Approved"
            elif action == "delete":
                emailer.delete(conn, email_id, u.id)
                msg = "Deleted"
            elif action == "edit":
                emailer.edit(conn, email_id, subject, body.replace("\r\n", "\n"), u.id)
                msg = "Saved"
            elif action == "skip":
                msg = "Skipped for now"
            else:
                raise HTTPException(404)
        except emailer.EmailError as e:
            return outreach_reload(request, conn, str(e), "error")
        return outreach_reload(request, conn, msg)

    @app.post("/outreach/send", response_class=HTMLResponse)
    def outreach_send(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        if not settings.smtp_configured:
            return outreach_reload(request, conn, "Set SMTP_HOST and FROM_EMAIL in .env first", "error")
        approved = emailer.drafts(conn, "approved")
        blocked = [e for e in approved if e["block_reason"]]
        sendable = [e for e in approved if not e["block_reason"]]
        if not sendable:
            msg = "Nothing can be sent" + (f": {blocked[0]['block_reason']}" if blocked else "")
            return outreach_reload(request, conn, msg, "error")

        def worker() -> None:
            c2 = open_conn()
            try:
                emailer.send(c2, settings, clients(), me(request).id)
            finally:
                c2.close()

        threading.Thread(target=worker, daemon=True).start()
        return outreach_reload(request, conn, f"Sending {len(sendable)} email(s) in the background, 60-180s apart. "
                                              f"{len(blocked)} blocked.")

    @app.post("/outreach/suppress", response_class=HTMLResponse)
    def outreach_suppress(request: Request, email: str = Form(...), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        try:
            emailer.suppress(conn, email, me(request).id)
        except emailer.EmailError as e:
            return outreach_reload(request, conn, str(e), "error")
        return outreach_reload(request, conn, f"{email} suppressed")

    @app.post("/outreach/unsuppress", response_class=HTMLResponse)
    def outreach_unsuppress(request: Request, email: str = Form(...), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        emailer.unsuppress(conn, email)
        return outreach_reload(request, conn, f"{email} removed from suppression")

    @app.post("/outreach/paste-optouts", response_class=HTMLResponse)
    def outreach_optouts(request: Request, text: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        found = emailer.extract_addresses(text)
        for e in found:
            emailer.suppress(conn, e, me(request).id, "Replied STOP/unsubscribe")
        return outreach_reload(request, conn, f"Suppressed {len(found)} address(es)")

    # ======================================================================
    # Preview sites
    # ======================================================================

    @app.get("/previews", response_class=HTMLResponse)
    def previews(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        return render(request, conn, "previews.html", active="previews", title="Preview Sites",
                      items=preview_mod.list_previews(conn, settings), can_publish=bool(settings.netlify_auth_token),
                      leads_for_select=leads_mod.list_leads(conn, top=200, sort="score"))

    @app.post("/previews/{lead_id}/generate")
    def preview_generate(request: Request, lead_id: int, back: str = Form("/previews"),
                         conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        preview_mod.generate(conn, settings, lead_id, me(request).id)
        if request.headers.get("HX-Request"):
            return Response(status_code=204, headers={**toast("Preview generated"), "HX-Refresh": "true"})
        return RedirectResponse(back if back.startswith("/") else "/previews", status_code=303)

    @app.get("/previews/{lead_id}/view")
    def preview_view(lead_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        lead = leads_mod.get_lead(conn, lead_id)
        if not lead or not lead.get("preview_slug"):
            raise HTTPException(404)
        path = preview_mod.preview_dir(settings, lead["preview_slug"]) / "index.html"
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="text/html")

    @app.get("/previews/{lead_id}/export")
    def preview_export(lead_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        try:
            name, data = preview_mod.export_zip(conn, settings, lead_id)
        except LeadError:
            raise HTTPException(404) from None
        return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/previews/{lead_id}/delete")
    def preview_delete(request: Request, lead_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        preview_mod.delete(conn, settings, lead_id, me(request).id)
        return RedirectResponse("/previews", status_code=303)

    @app.post("/previews/{lead_id}/publish")
    def preview_publish(request: Request, lead_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        try:
            url = preview_mod.publish(conn, settings, clients(), lead_id, me(request).id)
        except LeadError as e:
            return Response(status_code=204, headers=toast(str(e), "error"))
        return Response(status_code=204, headers={**toast(f"Published: {url}"), "HX-Refresh": "true"})

    # ======================================================================
    # Objections
    # ======================================================================

    @app.get("/objections", response_class=HTMLResponse)
    def objections(request: Request, period: str = "month", conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        period = period if period in stats_mod.PERIODS else "month"
        b = stats_mod.period_bounds(period)
        counts = calls_mod.objection_counts(conn, b["start"])
        tags = calls_mod.objection_tags(conn)
        label = {t["key"]: t["label"] for t in tags}
        by_trade: dict[str, dict[str, int]] = {}
        args: list[Any] = []
        sql = "SELECT l.trade, o.tag, COUNT(*) AS n FROM call_objections o JOIN leads l ON l.id=o.lead_id"
        if b["start"]:
            sql += " WHERE o.created_at >= ?"
            args.append(b["start"])
        for r in conn.execute(sql + " GROUP BY l.trade, o.tag", args).fetchall():
            by_trade.setdefault(r["trade"] or "-", {})[r["tag"]] = r["n"]
        # Trend: objections per week, last 12 weeks.
        weeks = []
        t = today_london()
        start_week = t - timedelta(days=t.weekday())
        for i in range(11, -1, -1):
            ws = start_week - timedelta(weeks=i)
            s, e = stats_mod.period_bounds("week", ws)["start"], stats_mod.period_bounds("week", ws)["end"]
            n = conn.execute("SELECT COUNT(*) FROM call_objections WHERE created_at>=? AND created_at<?", (s, e)).fetchone()[0]
            weeks.append({"label": ws.strftime("%d %b"), "n": n})
        latest: dict[str, list[dict[str, Any]]] = {}
        for r in conn.execute(
            """SELECT o.tag, o.other_text, c.note, l.name, l.id AS lead_id, o.created_at, u.initials FROM call_objections o
               JOIN calls c ON c.id=o.call_id JOIN leads l ON l.id=o.lead_id LEFT JOIN users u ON u.id=o.user_id
               ORDER BY o.id DESC LIMIT 200"""
        ).fetchall():
            if len(latest.setdefault(r["tag"], [])) < 4:
                latest[r["tag"]].append(dict(r))
        return render(request, conn, "objections.html", active="objections", title="Objections", period=period,
                      periods=stats_mod.PERIODS, counts=counts, tags=tags, label=label, by_trade=by_trade, weeks=weeks,
                      latest=latest, notes=calls_mod.objection_notes(conn))

    @app.post("/objections/{tag}/note")
    def objection_note(request: Request, tag: str, rebuttal: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        calls_mod.set_objection_note(conn, tag, rebuttal, me(request).id)
        return Response(status_code=204, headers=toast("Rebuttal saved"))

    # ======================================================================
    # Do Not Call
    # ======================================================================

    @app.get("/dnc", response_class=HTMLResponse)
    def dnc_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        return render(request, conn, "dnc.html", active="dnc", title="Do Not Call", rows=leads_mod.dnc_list(conn), error=None)

    @app.post("/dnc/add")
    def dnc_add(request: Request, phone: str = Form(...), reason: str = Form(""), do_not_email: str | None = Form(None),
                conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        email = None
        if do_not_email:
            row = conn.execute("SELECT email FROM leads WHERE phone_e164=? AND email IS NOT NULL",
                               (phones.normalise_e164(phone),)).fetchone()
            email = row["email"] if row else None
        try:
            leads_mod.dnc_add(conn, phone, reason or None, me(request).id, also_email=email)
        except LeadError as e:
            return render(request, conn, "dnc.html", status_code=422, active="dnc", title="Do Not Call",
                          rows=leads_mod.dnc_list(conn), error=str(e))
        return RedirectResponse("/dnc", status_code=303)

    @app.post("/dnc/remove")
    def dnc_remove(request: Request, phone: str = Form(...), confirm: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        if confirm != "yes":
            raise HTTPException(400, "Removal must be confirmed.")
        leads_mod.dnc_remove(conn, phone, me(request).id)
        return RedirectResponse("/dnc", status_code=303)

    # ======================================================================
    # Backups
    # ======================================================================

    @app.get("/backups", response_class=HTMLResponse)
    def backups(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        return render(request, conn, "backups.html", active="backups", title="Backups", info=backup_mod.backup_info(settings),
                      message=request.query_params.get("msg"))

    @app.post("/backups/now")
    def backup_now(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        b = backup_mod.make_backup(conn, settings)
        return RedirectResponse(f"/backups?msg={quote('Backup saved: ' + b.name)}", status_code=303)

    @app.post("/backups/restore")
    def backup_restore(request: Request, name: str = Form(...), confirm: str = Form(""),
                       conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        try:
            safety = backup_mod.restore(conn, settings, name, confirm)
        except (ValueError, FileNotFoundError) as e:
            return RedirectResponse(f"/backups?msg={quote(str(e))}", status_code=303)
        return RedirectResponse(f"/backups?msg={quote(f'Restored {name}. Safety backup: {safety.name}')}", status_code=303)

    # ======================================================================
    # Settings
    # ======================================================================

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        guard = UsageGuard(conn, settings)
        script = settings.template_file("call_script.md")
        tmpl = settings.template_file("cold_email.txt")
        return render(
            request, conn, "settings.html", active="settings", title="Settings",
            keys=settings.key_status(), script=script.read_text(encoding="utf-8"), template=tmpl.read_text(encoding="utf-8"),
            send_limit=emailer.daily_limit(conn, settings), meeting_minutes=calls_mod.meeting_minutes(conn),
            tags=calls_mod.objection_tags(conn), meter=guard.meter(), by_user=usage_by_user(conn), by_search=usage_by_search(conn),
            free_tier=settings.free_tier_mode, budget=settings.monthly_api_budget_gbp, spent=guard.spent_this_month_gbp(),
            message=request.query_params.get("msg"), pw_error=request.query_params.get("pwerr"),
        )

    def back(msg: str) -> Response:
        return RedirectResponse(f"/settings?msg={quote(msg)}", status_code=303)

    def write_template(name: str, text: str) -> None:
        d = settings.templates_dir
        d.mkdir(parents=True, exist_ok=True)
        with (d / name).open("w", encoding="utf-8", newline="\n") as f:
            f.write(text.replace("\r\n", "\n"))

    @app.post("/settings/target")
    def set_target(request: Request, target: int = Form(...), user_id: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        uid = int(user_id) if user_id else me(request).id
        conn.execute("UPDATE users SET daily_call_target=? WHERE id=?", (max(1, target), uid))
        conn.commit()
        return back("Daily call target saved")

    @app.post("/settings/script")
    def set_script(script: str = Form("")) -> Response:
        write_template("call_script.md", script)
        return back("Call script saved")

    @app.post("/settings/template")
    def set_template(request: Request, template: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        try:
            r = emailer.render(template, {"name": "Test Ltd", "trade": "plumbers", "town": "Crawley",
                                          "hook": "120 reviews at 4.8 stars but no website", "preview_url": "https://example.netlify.app"}, settings)
        except emailer.EmailError as e:
            return back(str(e))
        if r.words > emailer.MAX_WORDS:
            return back(f"Template is {r.words} words; keep it under {emailer.MAX_WORDS}. Not saved.")
        write_template("cold_email.txt", template)
        return back("Email template saved")

    @app.post("/settings/limits")
    def set_limits(send_limit: int = Form(...), meeting_minutes: int = Form(30), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        db.set_setting(conn, "daily_send_limit", max(0, send_limit))
        db.set_setting(conn, "meeting_minutes", max(5, meeting_minutes))
        return back("Saved")

    @app.post("/settings/objections")
    def set_objections(tags: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        out = []
        for line in tags.replace("\r\n", "\n").split("\n"):
            label = line.strip()
            if not label:
                continue
            key = preview_mod.slugify(label).replace("-", "_")
            existing = next((t for t in calls_mod.objection_tags(conn) if t["label"] == label), None)
            out.append({"key": existing["key"] if existing else key, "label": label})
        if not any(t["key"] == "other" for t in out):
            out.append({"key": "other", "label": "Other"})
        db.set_setting(conn, "objection_tags", out)
        return back("Objection tags saved")

    @app.post("/settings/password")
    def change_password(request: Request, current: str = Form(""), new: str = Form(""), again: str = Form(""),
                        conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        if new != again:
            return RedirectResponse("/settings?pwerr=" + quote("The new passwords don't match."), status_code=303)
        try:
            users.change_password(conn, me(request).id, current, new)
        except users.AuthError as e:
            return RedirectResponse("/settings?pwerr=" + quote(str(e)), status_code=303)
        return back("Password changed")

    # ======================================================================
    # My grafting
    # ======================================================================

    @app.get("/grafting", response_class=HTMLResponse)
    def grafting(request: Request, user: int | None = None, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        target = users.get_user(conn, user) if user else me(request)
        if target is None:
            raise HTTPException(404)
        return render(request, conn, "grafting.html", active="grafting", title="My grafting" if target.id == me(request).id
                      else f"{target.first_name}'s grafting", target=target, g=stats_mod.grafting(conn, target.id),
                      types=calls_mod.MANUAL_TYPES, today=today_london().isoformat())

    @app.post("/grafting/entry")
    def grafting_entry(request: Request, entry_type: str = Form(...), count: int = Form(1), minutes: int = Form(0),
                       note: str = Form(""), entry_date: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        try:
            calls_mod.add_manual_entry(conn, me(request).id, entry_type, count, minutes, note or None, entry_date or None)
        except LeadError as e:
            raise HTTPException(422, str(e)) from None
        return RedirectResponse("/grafting", status_code=303)

    # ======================================================================
    # Partials
    # ======================================================================

    @app.get("/partials/recent", response_class=HTMLResponse)
    def recent_partial(request: Request, scope: str = "me", conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        u = me(request)
        resp = render(request, conn, "partials/recent.html", partial=True, recent_scope=scope,
                      recent=stats_mod.recent_items(conn, None if scope == "team" else [u.id]))
        resp.set_cookie("lf_recent", "team" if scope == "team" else "me", samesite="lax", httponly=True)
        return resp

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        if request.headers.get("HX-Request"):
            return PlainTextResponse(str(exc.detail), status_code=exc.status_code,
                                     headers=toast(str(exc.detail), "error"))
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    return app
