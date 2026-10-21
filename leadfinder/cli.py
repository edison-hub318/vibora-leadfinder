"""The `leadfinder` command line. Every command calls the same functions the dashboard uses."""
from __future__ import annotations

import sys
import webbrowser
from pathlib import Path
from typing import Callable, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn
from rich.table import Table

from . import (audit as audit_mod, backup as backup_mod, calls as calls_mod, campaigns as camp_mod, db, emailer, enrich,
               leads as leads_mod, meetings as meetings_mod, phones, preview as preview_mod, search as search_mod,
               stats as stats_mod, users)
from .clients import Clients
from .config import Settings, load_settings
from .freetier import UsageGuard
from .leads import LeadError
from .timeutil import fmt_local, now_london, parse_local
from .trades import load_trades, resolve_trade, save_new_trade

# safe_box + no emoji: renders in Windows Terminal and the old PowerShell console alike.
console = Console(safe_box=True, emoji=False, highlight=False)
err = Console(stderr=True, safe_box=True, emoji=False, highlight=False)

app = typer.Typer(help="Vibora lead finder: find, call and email local businesses with weak websites.",
                  no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False)
user_app = typer.Typer(help="Manage accounts.", no_args_is_help=True)
dnc_app = typer.Typer(help="Do Not Call list.", no_args_is_help=True)
backup_app = typer.Typer(help="Backups (also run automatically once a day).", invoke_without_command=True)
app.add_typer(user_app, name="user")
app.add_typer(dnc_app, name="dnc")
app.add_typer(backup_app, name="backup")
campaign_app = typer.Typer(help="Batch cold email: one template to a whole segment of leads.", no_args_is_help=True)
app.add_typer(campaign_app, name="campaign")

STATUS_STYLE = {
    "none": "bold red", "social_only": "red", "free_builder": "yellow", "poor": "yellow",
    "ok": "cyan", "good": "green", None: "dim",
}

# Tests swap these for fakes.
_clients_factory: Callable[[Settings], Clients] = Clients.real
_settings_home: Optional[Path] = None


class Ctx:
    def __init__(self) -> None:
        self.settings = load_settings(_settings_home)
        self.conn = db.connect(self.settings.db_path)
        self._clients: Clients | None = None

    @property
    def clients(self) -> Clients:
        if self._clients is None:
            self._clients = _clients_factory(self.settings)
        return self._clients

    def user(self) -> users.User | None:
        return users.cli_current_user(self.conn, self.settings)

    def require_user(self) -> users.User:
        u = self.user()
        if u is None:
            if users.user_count(self.conn) == 0:
                fail('No accounts yet. Create one with:  leadfinder user add "Your Name"')
            fail('Nobody is logged in. Run:  leadfinder login "Your Name"')
        return u  # type: ignore[return-value]


_ctx: Ctx | None = None


def ctx() -> Ctx:
    global _ctx
    if _ctx is None:
        _ctx = Ctx()
    return _ctx


def reset_ctx() -> None:
    global _ctx
    if _ctx is not None:
        _ctx.conn.close()
    _ctx = None


def fail(msg: str, code: int = 1) -> None:
    err.print(f"[bold red]Error:[/] {msg}")
    raise typer.Exit(code)


@app.callback()
def _main(ctx_: typer.Context) -> None:
    """Runs before every command: daily automatic backup."""
    if ctx_.invoked_subcommand in (None, "user", "login", "whoami"):
        return
    c = ctx()
    made = backup_mod.ensure_daily_backup(c.conn, c.settings)
    if made is not None and c.settings.backup_dir_is_default:
        err.print("[dim]Daily backup saved to ./backups (set BACKUP_DIR in .env to use OneDrive/Google Drive).[/]")


def main() -> None:
    try:
        app()
    finally:
        reset_ctx()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chip(status: str | None) -> str:
    label = {"none": "no website", "social_only": "social only", "free_builder": "free builder", None: "unchecked"}.get(status, status or "")
    return f"[{STATUS_STYLE.get(status, 'dim')}]{label}[/]"


def _rating(lead: dict) -> str:
    if lead.get("rating") is None:
        return f"- ({lead.get('review_count') or 0})"
    return f"{float(lead['rating']):.1f} ({lead.get('review_count') or 0})"


def _lead_table(rows: list[dict], title: str | None = None) -> Table:
    t = Table(title=title, show_lines=False, header_style="bold", safe_box=True)
    for col, kw in [("ID", {"justify": "right"}), ("Name", {}), ("Trade", {}), ("Phone", {}), ("Website", {}),
                    ("Rating", {"justify": "right"}), ("Score", {"justify": "right"}), ("Status", {}), ("Callback", {})]:
        t.add_column(col, **kw)
    for r in rows:
        t.add_row(
            str(r["id"]), r["name"], (r.get("trade") or "").replace("_", " "), phones.pretty(r.get("phone")) or "-",
            _chip(r.get("website_status") if r.get("website") else "none"), _rating(r), str(r.get("score", 0)),
            r.get("status", ""), fmt_local(r.get("callback_at")) if r.get("callback_at") else "",
        )
    return t


def _parse_when(value: Optional[str], what: str):
    if not value:
        return None
    try:
        return parse_local(value)
    except ValueError:
        fail(f"Couldn't read the {what} '{value}'. Use 2026-10-20 or 2026-10-20T14:00.")


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def _prompt_new_password() -> str:
    while True:
        pw = typer.prompt("Password (min 8 characters)", hide_input=True)
        if len(pw) < users.MIN_PASSWORD:
            err.print(f"[red]Too short: at least {users.MIN_PASSWORD} characters.[/]")
            continue
        again = typer.prompt("Type it again", hide_input=True)
        if pw != again:
            err.print("[red]Those didn't match. Try again.[/]")
            continue
        return pw


@user_app.command("add")
def user_add(full_name: str = typer.Argument(..., help='e.g. "Edison Young"'),
             target: int = typer.Option(40, help="Daily call target")) -> None:
    """Create an account (you'll be asked for a password twice)."""
    c = ctx()
    if users.get_user_by_name(c.conn, full_name):
        fail(f"An account for '{users.tidy_name(full_name)}' already exists.")
    pw = _prompt_new_password()
    try:
        u = users.add_user(c.conn, full_name, pw, target)
    except users.AuthError as e:
        fail(str(e))
    console.print(f"[green]Created account for {u.full_name} ({u.initials}).[/]")


@user_app.command("passwd")
def user_passwd(full_name: str) -> None:
    """Reset someone's password."""
    c = ctx()
    u = users.get_user_by_name(c.conn, full_name)
    if not u:
        fail(f"No account called '{full_name}'.")
    users.set_password(c.conn, u.id, _prompt_new_password())
    console.print(f"[green]Password updated for {u.full_name}.[/]")


@user_app.command("list")
def user_list() -> None:
    """List accounts."""
    c = ctx()
    t = Table(header_style="bold", safe_box=True)
    for col in ("ID", "Name", "Initials", "Daily target", "Last login"):
        t.add_column(col)
    for u in users.list_users(c.conn):
        t.add_row(str(u["id"]), u["full_name"], u["initials"], str(u["daily_call_target"]), fmt_local(u["last_login"]) or "never")
    console.print(t)


@app.command()
def login(full_name: str) -> None:
    """Log in on this PC so your CLI actions are attributed to you."""
    c = ctx()
    pw = typer.prompt("Password", hide_input=True)
    try:
        u = users.authenticate(c.conn, full_name, pw)
    except users.AuthError as e:
        fail(str(e))
    users.cli_login(c.settings, u)
    console.print(f"Logged in as [bold]{u.full_name}[/].")


@app.command()
def logout() -> None:
    """Log out of the CLI on this PC."""
    users.cli_logout(ctx().settings)
    console.print("Logged out.")


@app.command()
def whoami() -> None:
    """Show who the CLI is logged in as."""
    u = ctx().user()
    console.print(f"{u.full_name} ({u.initials})" if u else "Nobody is logged in.")


# ---------------------------------------------------------------------------
# Search, audit, refresh
# ---------------------------------------------------------------------------

@app.command()
def search(
    trade: str = typer.Argument(..., help='What to search for, e.g. "plumbers"'),
    town: str = typer.Option(..., "--town", "-t", help='Town or postcode district, e.g. "Crawley" or "RH10"'),
    max_leads: int = typer.Option(60, "--max", help="How many NEW callable leads you want"),
    radius: float = typer.Option(8.0, "--radius", help="Search radius in km"),
    include_no_phone: bool = typer.Option(False, "--include-no-phone", help="Keep leads with no phone number"),
    no_audit: bool = typer.Option(False, "--no-audit", help="Skip the website audit and email lookup"),
    force: bool = typer.Option(False, "--force", help="Search again even if done in the last 14 days"),
    budget: Optional[int] = typer.Option(None, "--budget", help="Max Google requests for this search"),
    save_trade: bool = typer.Option(False, "--save-trade", help="Add an unknown trade to trades.yaml"),
    country: str = typer.Option("GB", "--country", help='GB (UK, default) or US, e.g. --country US --town "Austin, TX"'),
) -> None:
    """Find local businesses and rank them as leads."""
    c = ctx()
    user = c.require_user()
    t = resolve_trade(c.settings, trade)
    if not t.known:
        console.print(f"[yellow]'{trade}' isn't in trades.yaml: searching for it as typed, without the category check.[/]")
        if save_trade or (sys.stdin.isatty() and typer.confirm("Save it to trades.yaml for next time?", default=False)):
            save_new_trade(c.settings, trade)
            console.print("[green]Saved to trades.yaml.[/]")
    guard = UsageGuard(c.conn, c.settings)
    try:
        country = search_mod.norm_country(country)
    except search_mod.SearchError as e:
        fail(str(e))
    plan = search_mod.planned_requests(c.conn, guard, town, max_leads, budget, country)
    st = guard.status("text_search_enterprise")
    console.print(
        f"Planned: about [bold]{plan['text_search']}[/] Text Search request(s) (max {plan['cap']})"
        + (" + 1 town lookup" if plan["geocode"] else "")
        + f". This month: {st.used}/{st.free} used" + (" (free-tier mode)" if c.settings.free_tier_mode else "") + "."
    )
    progress = Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), console=console, transient=True)
    tasks: dict[str, int] = {}

    def on(event: str, data: dict) -> None:
        if event == "page":
            console.print(f"  [dim]{data['query']} (page {data['page']}): {data['qualified']} qualified so far[/]")
        elif event == "ai_plan" and data["calls"]:
            console.print(f"  AI website judgement: up to {data['calls']} call(s) (AI_MAX_PER_SEARCH={c.settings.ai_max_per_search}).")
        elif event == "audit":
            if "audit" not in tasks:
                progress.start()
                tasks["audit"] = progress.add_task("Auditing websites", total=data["total"])
            progress.update(tasks["audit"], completed=data["done"])
        elif event == "enrich":
            if "enrich" not in tasks:
                tasks["enrich"] = progress.add_task("Looking for emails", total=data["total"])
            progress.update(tasks["enrich"], completed=data["done"])

    opts = search_mod.SearchOptions(trade=trade, town=town, max_leads=max_leads, radius_km=radius,
                                    include_no_phone=include_no_phone, audit=not no_audit, force=force,
                                    request_cap=budget, country=country)
    try:
        rep = search_mod.run_search(c.conn, c.settings, c.clients, user, opts, progress=on)
    except search_mod.RepeatSearch as e:
        fail(str(e))
    except search_mod.SearchError as e:
        fail(str(e))
    finally:
        progress.stop()
    print_search_report(rep)


def print_search_report(rep: search_mod.SearchReport) -> None:
    funnel = "  ->  ".join(f"{label.lower()} {n}" for label, n in rep.funnel)
    console.print(Panel.fit(f"{funnel}\n= [bold]qualified {rep.qualified}[/]", title=f"{rep.trade} in {rep.town}", safe_box=True))
    if rep.closed_skipped:
        console.print(f"[dim]{rep.closed_skipped} closed businesses skipped.[/]")
    if rep.handled:
        nice = {"client": "clients/won", "dnc": "Do Not Call", "tps": "TPS-registered", "not_interested": "not interested (90 days)",
                "partner": "your partner's (active)", "known": "already in the database"}
        console.print("Already handled: " + ", ".join(f"{nice.get(k, k)} {v}" for k, v in rep.handled.items()))
    console.print(f"Found {rep.raw_count}, new {rep.qualified}, already known {rep.known_count}.")
    w = rep.website
    console.print(
        f"Websites: no website {w.get('none', 0)}, social-only {w.get('social_only', 0)}, free builder {w.get('free_builder', 0)}, "
        f"poor {w.get('poor', 0)}, decent {w.get('ok', 0) + w.get('good', 0)}"
        + (f", unchecked {w.get('unchecked', 0)}" if w.get("unchecked") else "")
    )
    console.print(f"Google requests: {rep.requests_made}. Estimated cost: £{rep.est_cost_gbp:.2f}.")
    reason = {
        "enough": f"Stopped: found the {rep.requested} leads you asked for.",
        "budget": rep.stop_message,
        "allowance": rep.stop_message,
        "exhausted": f"Stopped: {rep.stop_message} Found {rep.qualified} of {rep.requested}.",
        "error": f"Stopped early: {rep.stop_message}",
    }.get(rep.stop_reason, rep.stop_message)
    console.print(f"[bold]{reason}[/]")
    if rep.top:
        console.print(_lead_table(rep.top, title="Top 10 new leads"))


@app.command()
def audit(limit: int = typer.Option(50, "--limit"), recheck: bool = typer.Option(False, "--recheck")) -> None:
    """Audit websites of older leads (searches do this automatically)."""
    c = ctx()
    c.require_user()
    with Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), console=console, transient=True) as p:
        task = p.add_task("Auditing websites", total=None)
        counts = audit_mod.audit_command(c.conn, c.settings, c.clients, limit, recheck,
                                         progress=lambda d, t: p.update(task, completed=d, total=t))
    console.print("Audited: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in counts.items() if v) if any(counts.values()) else "Nothing to audit.")


@app.command()
def refresh(older_than: int = typer.Option(30, "--older-than", help="Days since last refresh"),
            limit: Optional[int] = typer.Option(None, "--limit")) -> None:
    """Re-fetch Google details for active leads (stays inside the free Place Details allowance)."""
    c = ctx()
    user = c.require_user()
    rep = search_mod.refresh_leads(c.conn, c.settings, c.clients, user, older_than, limit)
    console.print(f"Refreshed {rep.refreshed} of {rep.candidates} lead(s); {rep.closed} now closed on Google; {rep.failed} failed.")
    if rep.stopped:
        console.print(f"[yellow]{rep.stopped}[/]")


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

@app.command("list")
def list_cmd(
    town: Optional[str] = typer.Option(None, "--town"),
    trade: Optional[str] = typer.Option(None, "--trade"),
    status: Optional[str] = typer.Option(None, "--status", help="new|called|callback|interested|not_interested|client"),
    no_website: bool = typer.Option(False, "--no-website"),
    top: int = typer.Option(25, "--top"),
) -> None:
    """List leads, best first."""
    c = ctx()
    if trade:
        trade = resolve_trade(c.settings, trade).key
    rows = leads_mod.list_leads(c.conn, town=town, trade=trade, status=status, no_website=no_website, top=top)
    if not rows:
        console.print("No leads match.")
        return
    console.print(_lead_table(rows))


@app.command()
def show(lead_id: int) -> None:
    """Full details for one lead, plus call history."""
    c = ctx()
    try:
        lead = leads_mod.require_lead(c.conn, lead_id)
    except LeadError as e:
        fail(str(e))
    lines = [
        f"[bold]{lead['name']}[/]  (ID {lead['id']}, score {lead['score']})",
        f"{(lead['trade'] or '').replace('_', ' ')} in {lead['town']}  |  status: {lead['status']}  |  owner: {lead.get('owner_name') or '-'}",
        f"Phone: {phones.pretty(lead['phone']) or '-'} ({lead['phone_type']})   TPS: {lead['tps_status']}",
        f"Address: {lead['address']}",
        f"Website: {lead['website'] or '-'}  [{_chip(lead['website_status'] if lead['website'] else 'none')}]"
        + (f"  PageSpeed {lead['pagespeed_score']}" if lead.get("pagespeed_score") is not None else ""),
        f"Rating: {_rating(lead)}   Entity: {lead['entity_type'].replace('_', ' ')}   Email: {lead['email'] or '-'}",
        f"Hook: {leads_mod.display_hook(lead)}",
        f"Maps: {lead['maps_url'] or '-'}",
    ]
    if lead["website_issues"]:
        lines.append("Issues: " + "; ".join(lead["website_issues"]))
    if lead.get("existing_agency"):
        lines.append(f"[yellow]Existing agency credit: {lead['existing_agency']} (may be in a contract)[/]")
    if lead.get("quality_flags"):
        lines.append("Flags: " + ", ".join(f.replace("_", " ") for f in lead["quality_flags"]))
    if lead.get("callback_at"):
        lines.append(f"Callback: {fmt_local(lead['callback_at'])}")
    if lead.get("preview_url"):
        lines.append(f"Preview: {lead['preview_url']}")
    console.print(Panel("\n".join(lines), safe_box=True))
    hist = leads_mod.call_history(c.conn, lead_id)
    if hist:
        t = Table(title="Call history", header_style="bold", safe_box=True)
        for col in ("When", "Who", "Outcome", "Duration", "Note"):
            t.add_column(col)
        for h in hist:
            t.add_row(fmt_local(h["created_at"]), h.get("initials") or "", h["outcome"].replace("_", " "),
                      f"{h['duration_seconds'] // 60}:{h['duration_seconds'] % 60:02d}",
                      (h.get("note") or "") + (f" [objections: {h['objections']}]" if h.get("objections") else ""))
        console.print(t)
    else:
        console.print("[dim]No calls yet.[/]")


@app.command("next")
def next_cmd() -> None:
    """The single best lead to call right now."""
    c = ctx()
    user = c.require_user()
    lead = leads_mod.next_lead(c.conn, user.id)
    if not lead:
        console.print("Nothing to call right now. Run a search, or check callbacks with `leadfinder list --status callback`.")
        return
    warn = leads_mod.tps_warning(lead)
    body = [
        f"[bold]{lead['name']}[/]   (ID {lead['id']}, score {lead['score']})",
        f"[bold]{phones.pretty(lead['phone'])}[/]   {lead['maps_url'] or ''}",
        f"Hook: {leads_mod.display_hook(lead)}",
        f"Rating: {_rating(lead)}   Website: {_chip(lead['website_status'] if lead['website'] else 'none')}",
    ]
    if lead["status"] == "callback":
        body.insert(1, f"[yellow]Callback due {fmt_local(lead['callback_at'])}[/]")
    if lead.get("existing_agency"):
        body.append(f"[yellow]Site credits an agency: {lead['existing_agency']}[/]")
    body.append("")
    body.append(f"Opener: \"{leads_mod.call_opener(lead, c.settings.my_name)}\"")
    if warn:
        body.insert(0, f"[bold yellow]WARNING: {warn}[/]  (leadfinder tps {lead['id']} --clear / --registered)")
    console.print(Panel("\n".join(body), title="Next call", safe_box=True))
    console.print(f"[dim]Log it with: leadfinder log {lead['id']} --outcome no_answer|gatekeeper|connected|callback|meeting_booked|not_interested|wrong_number[/]")


@app.command()
def log(
    lead_id: int,
    outcome: str = typer.Option(..., "--outcome", "-o", help="no_answer|gatekeeper|connected|callback|meeting_booked|not_interested|wrong_number"),
    note: Optional[str] = typer.Option(None, "--note", "-n"),
    callback: Optional[str] = typer.Option(None, "--callback", help="2026-10-20 or 2026-10-20T14:00"),
    meeting: Optional[str] = typer.Option(None, "--meeting", help="2026-10-20T14:00"),
    duration: Optional[str] = typer.Option(None, "--duration", "-d", help="Call length: seconds or mm:ss"),
    objection: list[str] = typer.Option([], "--objection", help="Objection tag (repeatable), e.g. too_expensive"),
    objection_other: Optional[str] = typer.Option(None, "--objection-other", help="Free text for the 'other' tag"),
) -> None:
    """Record a call and update the lead."""
    c = ctx()
    user = c.require_user()
    if objection_other and "other" not in objection:
        objection = [*objection, "other"]
    valid = {t["key"] for t in calls_mod.objection_tags(c.conn)}
    bad = [o for o in objection if o not in valid]
    if bad:
        fail(f"Unknown objection tag(s): {', '.join(bad)}. Use: {', '.join(sorted(valid))}")
    try:
        res = calls_mod.log_call(
            c.conn, c.settings, lead_id, user.id, outcome, note=note,
            callback_at=_parse_when(callback, "callback date"), meeting_at=_parse_when(meeting, "meeting time"),
            duration_seconds=calls_mod.parse_duration(duration), objections=objection, objection_other=objection_other,
        )
    except LeadError as e:
        fail(str(e))
    lead = res["lead"]
    console.print(f"Logged [bold]{calls_mod.OUTCOMES[outcome]}[/] for {lead['name']}. Status: {lead['status']}.")
    if lead["status"] == "callback":
        console.print(f"Callback: {fmt_local(lead['callback_at'])}")
    if res["meeting_id"]:
        m = meetings_mod.get_meeting(c.conn, res["meeting_id"])
        console.print(f"Meeting: {fmt_local(m['start_at'])}. Calendar link:\n{meetings_mod.google_calendar_url(m)}")
        console.print("[dim]Or download an .ics file from the Meetings page (leadfinder meetings).[/]")


@app.command()
def stage(
    lead_id: int,
    to: str = typer.Argument(..., metavar="STAGE", help="proposal_sent|won|lost|meeting_booked"),
    setup: Optional[float] = typer.Option(None, "--setup", help="Setup fee (£) on won"),
    retainer: Optional[float] = typer.Option(None, "--retainer", help="Monthly retainer (£) on won"),
    reason: Optional[str] = typer.Option(None, "--reason", help="Why it was lost"),
) -> None:
    """Move a lead along the pipeline."""
    c = ctx()
    user = c.require_user()
    try:
        lead = calls_mod.set_stage(c.conn, lead_id, user.id, to, setup, retainer, reason)
    except LeadError as e:
        fail(str(e))
    console.print(f"{lead['name']} is now [bold]{lead['status'].replace('_', ' ')}[/].")


@app.command()
def tps(lead_id: int, registered: bool = typer.Option(..., "--registered/--clear",
                                                      help="Result of your TPS/CTPS check")) -> None:
    """Record whether you checked the number against the TPS/CTPS list."""
    c = ctx()
    user = c.require_user()
    try:
        leads_mod.set_tps(c.conn, lead_id, registered, user.id)
    except LeadError as e:
        fail(str(e))
    console.print("Marked TPS-registered: this lead won't be served for calling." if registered else "Marked TPS clear.")


@app.command()
def assign(lead_id: int, full_name: str) -> None:
    """Give a lead to a partner."""
    c = ctx()
    user = c.require_user()
    target = users.get_user_by_name(c.conn, full_name)
    if not target:
        fail(f"No account called '{full_name}'.")
    leads_mod.assign_owner(c.conn, lead_id, target.id, user.id)
    console.print(f"Lead {lead_id} assigned to {target.full_name}.")


@app.command()
def stats() -> None:
    """Calls today and this week, outcomes, conversion to interested."""
    c = ctx()
    s = stats_mod.cli_stats(c.conn)
    console.print(f"Calls today: [bold]{s['today']['dials']}[/] ({s['today']['connects']} connects)   "
                  f"This week: [bold]{s['week']['dials']}[/] ({s['week']['connects']} connects)")
    t = Table(title="Outcomes (all time)", header_style="bold", safe_box=True)
    t.add_column("Outcome")
    t.add_column("Count", justify="right")
    for o in s["outcomes"]:
        t.add_row(o["label"], str(o["n"]))
    console.print(t)
    conv = "-" if s["conversion"] is None else f"{s['conversion']:.1f}%"
    console.print(f"Conversion to interested: [bold]{conv}[/] ({s['interested_leads']} of {s['dialled_leads']} leads dialled)")


@app.command()
def export(
    path: Path = typer.Argument(Path("leads.csv")),
    town: Optional[str] = typer.Option(None, "--town"),
    trade: Optional[str] = typer.Option(None, "--trade"),
    status: Optional[str] = typer.Option(None, "--status"),
    no_website: bool = typer.Option(False, "--no-website"),
) -> None:
    """Export leads to CSV for Excel (Do Not Call and TPS-registered numbers are left out)."""
    c = ctx()
    if trade:
        trade = resolve_trade(c.settings, trade).key
    n = leads_mod.export_csv(c.conn, path, town=town, trade=trade, status=status, no_website=no_website)
    console.print(f"Exported {n} lead(s) to {path}.")


# ---------------------------------------------------------------------------
# Company type and email
# ---------------------------------------------------------------------------

@app.command()
def classify(limit: int = typer.Option(50, "--limit")) -> None:
    """Check Companies House to see which leads are limited companies."""
    c = ctx()
    user = c.require_user()
    if not c.settings.companies_house_enabled:
        console.print("COMPANIES_HOUSE_API_KEY isn't set in .env, so company types stay 'unknown'.")
        return
    counts = enrich.classify_command(c.conn, c.clients, user.id, limit)
    console.print("Classified: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in counts.items()))


@app.command("set-type")
def set_type(lead_id: int, entity: str = typer.Argument(..., help="limited_company|sole_trader")) -> None:
    """Override the company type by hand."""
    c = ctx()
    user = c.require_user()
    try:
        enrich.set_type(c.conn, lead_id, entity, user.id)
    except ValueError as e:
        fail(str(e))
    console.print(f"Lead {lead_id} set to {entity.replace('_', ' ')}.")


@app.command("add-email")
def add_email(lead_id: int, email: str) -> None:
    """Add an email address you found yourself."""
    c = ctx()
    user = c.require_user()
    try:
        emailer.add_email(c.conn, lead_id, email, user.id)
    except (emailer.EmailError, LeadError) as e:
        fail(str(e))
    console.print(f"Saved {email} on lead {lead_id}.")


@app.command("find-emails")
def find_emails(limit: int = typer.Option(20, "--limit")) -> None:
    """Look for a business email on each lead's own website (homepage and contact page)."""
    c = ctx()
    user = c.require_user()
    res = enrich.find_emails_command(c.conn, c.settings, c.clients, user.id, limit)
    found = [r for r in res if r["email"]]
    for r in found:
        console.print(f"  {r['name']}: {r['email']}  [dim]({r['source']})[/]")
    console.print(f"Found {len(found)} email(s) on {len(res)} site(s) checked.")


@app.command()
def draft(limit: int = typer.Option(20, "--limit")) -> None:
    """Write personalised first emails for review."""
    c = ctx()
    user = c.require_user()
    try:
        rep = emailer.draft(c.conn, c.settings, user.id, limit)
    except emailer.EmailError as e:
        fail(str(e))
    console.print(f"Drafted {len(rep.created)} email(s). Review them with: leadfinder review")
    for name, reason in rep.skipped:
        console.print(f"  [yellow]Skipped {name}: {reason}[/]")


@app.command()
def followup(limit: int = typer.Option(20, "--limit")) -> None:
    """Draft one follow-up for first emails sent 5+ days ago with no reply."""
    c = ctx()
    user = c.require_user()
    rep = emailer.followup(c.conn, c.settings, user.id, limit)
    console.print(f"Drafted {len(rep.created)} follow-up(s). Review them with: leadfinder review")
    for name, reason in rep.skipped:
        console.print(f"  [yellow]Skipped {name}: {reason}[/]")


@app.command()
def review() -> None:
    """Go through drafts one by one: a = approve, e = edit, s = skip, d = delete."""
    c = ctx()
    user = c.require_user()
    items = emailer.drafts(c.conn, "pending_review", c.settings)
    if not items:
        console.print("No drafts waiting. Create some with: leadfinder draft")
        return
    for i, e in enumerate(items, 1):
        console.print(Panel(f"To: {e['to_email']}\nSubject: {e['subject']}\n\n{e['body']}",
                            title=f"Draft {i} of {len(items)}: {e['name']} ({e['kind']})", safe_box=True))
        if e["block_reason"]:
            console.print(f"[yellow]Note: this one can't be sent as things stand: {e['block_reason']}.[/]")
        while True:
            choice = typer.prompt("[a]pprove, [e]dit, [s]kip, [d]elete, [q]uit", default="s").strip().lower()[:1]
            if choice == "a":
                emailer.approve(c.conn, e["id"], user.id)
                console.print("[green]Approved.[/]")
                break
            if choice == "e":
                edited = typer.edit(f"Subject: {e['subject']}\n\n{e['body']}")
                if edited is None:
                    console.print("No changes.")
                    continue
                first, _, rest = edited.replace("\r\n", "\n").partition("\n")
                subject = first.split(":", 1)[1].strip() if first.lower().startswith("subject:") else e["subject"]
                body = rest.strip("\n") if first.lower().startswith("subject:") else edited.strip("\n")
                try:
                    emailer.edit(c.conn, e["id"], subject, body, user.id)
                    e = {**e, "subject": subject, "body": body}
                    console.print("[green]Saved. Approve it now?[/]")
                except emailer.EmailError as ex:
                    console.print(f"[red]{ex}[/]")
                continue
            if choice == "s":
                break
            if choice == "d":
                emailer.delete(c.conn, e["id"], user.id)
                console.print("Deleted.")
                break
            if choice == "q":
                return
            console.print("Type a, e, s, d or q, then press Enter.")


@app.command()
def send(dry_run: bool = typer.Option(False, "--dry-run", help="Show exactly what would be sent, send nothing")) -> None:
    """Send approved emails (limited companies only, within the daily limit)."""
    c = ctx()
    user = c.require_user()
    if not dry_run and not c.settings.smtp_configured:
        fail("Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and FROM_EMAIL in .env first (or use --dry-run).")

    def on(event: str, data: dict) -> None:
        if event == "wait":
            console.print(f"[dim]Waiting {data['seconds']:.0f}s before the next email...[/]")
        elif event == "sent":
            console.print(f"Sent to {data['email']['to_email']}")

    rep = emailer.send(c.conn, c.settings, c.clients, user.id, dry_run=dry_run, on_event=on)
    if dry_run:
        for m in rep.would_send:
            console.print(Panel(m["message"], title=f"Would send (email {m['id']})", safe_box=True))
        console.print(f"[bold]Dry run:[/] {len(rep.would_send)} email(s) would be sent. Nothing was sent.")
    else:
        console.print(f"Sent {len(rep.sent)} email(s). Today: {rep.sent_before + len(rep.sent)}/{rep.limit}.")
    for reason, n in rep.skip_summary().items():
        console.print(f"  [yellow]Skipped {n}: {reason}[/]")
    if rep.stopped_at_limit:
        console.print(f"[yellow]Daily send limit ({rep.limit}) reached; the rest wait until tomorrow.[/]")
    for e, msg in rep.errors:
        console.print(f"  [red]Failed {e['to_email']}: {msg}[/]")


@app.command()
def suppress(email: str, reason: str = typer.Option("Opted out", "--reason")) -> None:
    """Never email this address again."""
    c = ctx()
    user = c.require_user()
    try:
        added = emailer.suppress(c.conn, email, user.id, reason)
    except emailer.EmailError as e:
        fail(str(e))
    console.print(f"{email} {'added to' if added else 'was already on'} the suppression list.")


@app.command("unsubscribe-check")
def unsubscribe_check() -> None:
    """Paste opt-out replies; their addresses go on the suppression list."""
    c = ctx()
    user = c.require_user()
    console.print("Paste the opt-out replies (or just the addresses). Finish with an empty line.")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    found = emailer.extract_addresses("\n".join(lines))
    if not found:
        console.print("No email addresses found.")
        return
    console.print("Found: " + ", ".join(found))
    if typer.confirm("Add all of these to the suppression list?", default=True):
        n = sum(1 for e in found if emailer.suppress(c.conn, e, user.id, "Replied STOP/unsubscribe"))
        console.print(f"Added {n} new address(es).")


@app.command()
def replied(lead_id: int) -> None:
    """Mark that a lead replied to your email (no follow-up will be drafted)."""
    c = ctx()
    user = c.require_user()
    emailer.mark_replied(c.conn, lead_id, user.id)
    console.print("Noted.")


@app.command()
def bounced(lead_id: int) -> None:
    """The last email to this lead bounced: suppress the address and count it in campaign stats."""
    c = ctx()
    user = c.require_user()
    try:
        addr = emailer.mark_bounced(c.conn, lead_id, user.id)
    except (emailer.EmailError, LeadError) as e:
        fail(str(e))
    console.print(f"Marked as bounced; {addr} is now suppressed.")


# ---------------------------------------------------------------------------
# Campaigns (batch cold email)
# ---------------------------------------------------------------------------

def _print_blocked(blocked: dict[str, int]) -> None:
    for reason, n in sorted(blocked.items(), key=lambda kv: -kv[1]):
        console.print(f"  [yellow]{n} blocked: {reason}[/]")


def _email_progress():
    progress = Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), console=console, transient=True)
    state: dict[str, int] = {}

    def on(done: int, total: int) -> None:
        if "t" not in state:
            progress.start()
            state["t"] = progress.add_task("Checking websites for emails", total=total)
        progress.update(state["t"], completed=done)
    return progress, on


def _print_draft_result(res: camp_mod.DraftResult, name: str) -> None:
    if res.emails_checked:
        console.print(f"Checked {res.emails_checked} website(s) for emails: found {res.emails_found}.")
    cnt = res.counts
    console.print(
        f"Segment: [bold]{cnt.get('matched', 0)}[/] leads match, {cnt.get('with_email', 0)} have an email address, "
        f"{cnt.get('available', 0)} not emailed yet."
    )
    if cnt.get("matched", 0) and cnt.get("with_email", 0) < cnt["matched"] / 2:
        console.print("[dim]Leads with no website rarely have a findable email: call those instead "
                      "(leadfinder next), or add addresses with add-email.[/]")
    console.print(f"Drafted [bold]{res.drafted}[/] email(s).")
    _print_blocked(res.blocked)
    for nm, reason in res.skipped[:10]:
        console.print(f"  [yellow]Skipped {nm}: {reason}[/]")
    for i, e in enumerate(res.samples, 1):
        console.print(Panel(f"To: {e['to_email']}\nSubject: {e['subject']}\n\n{e['body']}",
                            title=f"Sample {i} of {len(res.samples)}", safe_box=True))
    if res.drafted:
        console.print(f"Next: check the samples, then [bold]leadfinder campaign approve \"{name}\"[/]")


@campaign_app.command("create")
def campaign_create(
    name: str = typer.Argument(..., help='A short name, e.g. "hvac-austin-oct"'),
    country: str = typer.Option("GB", "--country", help="GB (UK) or US"),
    trade: Optional[str] = typer.Option(None, "--trade", help='e.g. "hvac" (blank = all trades)'),
    town: Optional[str] = typer.Option(None, "--town", help="blank = all towns"),
    website: Optional[str] = typer.Option(
        None, "--website", help="Comma list of website statuses. Default: none,social_only,free_builder,poor"),
    min_score: int = typer.Option(0, "--min-score"),
    template: Optional[str] = typer.Option(None, "--template", help="File in templates/ (default depends on country)"),
    followup_template: Optional[str] = typer.Option(None, "--followup-template"),
    limit: int = typer.Option(200, "--limit", help="Most drafts to create now (top up later with `campaign add`)"),
) -> None:
    """Draft one email per matching lead, using one template."""
    c = ctx()
    user = c.require_user()
    try:
        f = camp_mod.make_filters(country, trade, town, website, min_score)
        progress, on = _email_progress()
        try:
            res = camp_mod.create(c.conn, c.settings, user.id, name, f, template, followup_template, limit,
                                  clients=c.clients, progress=on)
        finally:
            progress.stop()
    except camp_mod.CampaignError as e:
        fail(str(e))
    console.print(f"[bold]Campaign '{name}'[/]: {f.describe()}")
    _print_draft_result(res, name)


@campaign_app.command("add")
def campaign_add(name: str, limit: int = typer.Option(200, "--limit")) -> None:
    """Top up a campaign with new matching leads (e.g. after more searches)."""
    c = ctx()
    user = c.require_user()
    progress, on = _email_progress()
    try:
        res = camp_mod.extend(c.conn, c.settings, user.id, name, limit, clients=c.clients, progress=on)
    except camp_mod.CampaignError as e:
        fail(str(e))
    finally:
        progress.stop()
    _print_draft_result(res, name)


@campaign_app.command("approve")
def campaign_approve(name: str, yes: bool = typer.Option(False, "--yes", help="Skip the confirmation")) -> None:
    """Approve every sendable draft in the campaign in one go (also resumes a paused one)."""
    c = ctx()
    user = c.require_user()
    try:
        camp = camp_mod.get(c.conn, name)
    except camp_mod.CampaignError as e:
        fail(str(e))
    waiting = camp_mod.pending(c.conn, camp["id"])
    if not waiting:
        console.print("Nothing waiting for approval.")
        return
    sample = waiting[0]
    console.print(Panel(f"To: {sample['to_email']}\nSubject: {sample['subject']}\n\n{sample['body']}",
                        title=f"Example of {len(waiting)} draft(s)", safe_box=True))
    if not yes and not typer.confirm(f"Approve all sendable drafts in '{camp['name']}'?", default=False):
        console.print("Nothing approved.")
        return
    res = camp_mod.approve(c.conn, c.settings, user.id, camp["id"])
    console.print(f"Approved [bold]{res.approved}[/]. Send with: leadfinder campaign send \"{camp['name']}\"")
    _print_blocked(res.blocked)


@campaign_app.command("send")
def campaign_send(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Send the campaign's approved emails, up to today's limit. Run it again each day until done."""
    c = ctx()
    user = c.require_user()
    if not dry_run and not c.settings.smtp_configured:
        fail("Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and FROM_EMAIL in .env first (or use --dry-run).")
    try:
        camp = camp_mod.get(c.conn, name)
    except camp_mod.CampaignError as e:
        fail(str(e))
    hour = now_london().hour
    if camp["country"] == "US" and not dry_run and not (13 <= hour <= 21):
        console.print("[yellow]It's night in the US right now. Emails sent between 13:00 and 21:00 UK time "
                      "land in US working hours and tend to get more replies.[/]")
        if not typer.confirm("Send anyway?", default=False):
            return

    def on(event: str, data: dict) -> None:
        if event == "wait":
            console.print(f"[dim]Waiting {data['seconds']:.0f}s before the next email...[/]")
        elif event == "sent":
            console.print(f"Sent to {data['email']['to_email']}")

    try:
        rep = camp_mod.send(c.conn, c.settings, c.clients, user.id, camp["id"], dry_run=dry_run, on_event=on)
    except (camp_mod.CampaignError, emailer.EmailError) as e:
        fail(str(e))
    if dry_run:
        for m in rep.would_send[:3]:
            console.print(Panel(m["message"], title=f"Would send (email {m['id']})", safe_box=True))
        console.print(f"[bold]Dry run:[/] {len(rep.would_send)} email(s) would go today. Nothing was sent.")
    else:
        console.print(f"Sent {len(rep.sent)}. Today: {rep.sent_before + len(rep.sent)}/{rep.limit}.")
    for reason, n in rep.skip_summary().items():
        console.print(f"  [yellow]Skipped {n}: {reason}[/]")
    left = camp_mod.stats(c.conn, camp["id"])["queued"]
    if rep.stopped_at_limit:
        console.print(f"[yellow]Daily limit reached. {left} still queued: run this again tomorrow.[/]")
    for e, msg in rep.errors:
        console.print(f"  [red]Failed {e['to_email']}: {msg}[/]")


@campaign_app.command("followup")
def campaign_followup(name: str, limit: int = typer.Option(200, "--limit")) -> None:
    """Draft one follow-up for campaign leads emailed 5+ days ago with no reply (then approve + send)."""
    c = ctx()
    user = c.require_user()
    try:
        rep = camp_mod.draft_followups(c.conn, c.settings, user.id, name, limit)
    except camp_mod.CampaignError as e:
        fail(str(e))
    console.print(f"Drafted {len(rep.created)} follow-up(s). Next: leadfinder campaign approve \"{name}\"")
    for nm, reason in rep.skipped:
        console.print(f"  [yellow]Skipped {nm}: {reason}[/]")


@campaign_app.command("pause")
def campaign_pause(name: str) -> None:
    """Stop sending: queued emails go back to awaiting approval."""
    c = ctx()
    user = c.require_user()
    try:
        n = camp_mod.pause(c.conn, user.id, name)
    except camp_mod.CampaignError as e:
        fail(str(e))
    console.print(f"Paused. {n} queued email(s) moved back to awaiting approval.")


@campaign_app.command("list")
def campaign_list() -> None:
    """All campaigns with their results."""
    c = ctx()
    rows = camp_mod.list_all(c.conn)
    if not rows:
        console.print('No campaigns yet. Start one with: leadfinder campaign create "name" --trade ... --country ...')
        return
    t = Table(safe_box=True)
    for col in ("Name", "Status", "Sent", "Queued", "Replies", "Meetings", "Meetings/100", "vs 2026 benchmark"):
        t.add_column(col)
    for r in rows:
        st = r["stats"]
        t.add_row(r["name"], r["status"], str(st["sent"]), str(st["queued"] + st["pending"]),
                  f"{st['replied']} ({st['reply_rate']}%)", str(st["meetings"]), str(st["meeting_rate"]),
                  st["meeting_band"])
    console.print(t)


@campaign_app.command("stats")
def campaign_stats(name: str) -> None:
    """The full funnel for one campaign, compared with 2026 cold email benchmarks."""
    c = ctx()
    try:
        camp = camp_mod.get(c.conn, name)
    except camp_mod.CampaignError as e:
        fail(str(e))
    st = camp_mod.stats(c.conn, camp["id"])
    console.print(f"[bold]{camp['name']}[/] ({camp['status']}): {camp['filters'].describe()}")
    console.print(f"Template: {camp['template']}   Follow-up: {camp['followup_template']}")
    t = Table(safe_box=True, show_header=False)
    t.add_column("")
    t.add_column("", justify="right")
    for label, val in [
        ("Awaiting approval", st["pending"]), ("Queued to send", st["queued"]),
        ("First emails sent", st["sent"]), ("Bounced", f"{st['bounced']} ({st['bounce_rate']}%)"),
        ("Delivered", st["delivered"]), ("Follow-ups sent", st["followups"]),
        ("Replied", f"{st['replied']} ({st['reply_rate']}%, {st['reply_band']})"),
        ("Meetings booked", f"{st['meetings']} ({st['meeting_rate']} per 100, {st['meeting_band']})"),
        ("Emails per meeting", st["emails_per_meeting"] or "-"), ("Won", st["won"]),
        ("Opted out", st["unsubscribed"]),
    ]:
        t.add_row(label, str(val))
    console.print(t)
    console.print("[dim]2026 benchmarks per 100 delivered: replies 1.5-3 average, 3-5 good; "
                  "meetings 0.3-1 average, 1-2 good, 2+ great.[/]")
    if st["delivered"] < 100:
        console.print("[dim]Under 100 delivered, these rates swing a lot: one meeting is a big jump.[/]")
    for w in camp_mod.health_warnings(st):
        console.print(f"[yellow]{w}[/]")


# ---------------------------------------------------------------------------
# Meetings, previews, DNC, backups
# ---------------------------------------------------------------------------

@app.command()
def meetings() -> None:
    """Upcoming meetings and past ones awaiting an outcome."""
    c = ctx()
    m = meetings_mod.list_meetings(c.conn)
    if not m["upcoming"] and not m["awaiting"]:
        console.print("No meetings booked.")
        return
    for title, rows in (("Upcoming", m["upcoming"]), ("Held? Record the outcome on the Meetings page", m["awaiting"])):
        if not rows:
            continue
        t = Table(title=title, header_style="bold", safe_box=True)
        for col in ("ID", "When", "Business", "Phone", "Who"):
            t.add_column(col)
        for r in rows:
            t.add_row(str(r["id"]), fmt_local(r["start_at"]), r["name"], phones.pretty(r["phone"]), r.get("initials") or "")
        console.print(t)


@app.command()
def preview(lead_id: int, publish: bool = typer.Option(False, "--publish", help="Deploy to Netlify")) -> None:
    """Generate a one-page demo website for a lead."""
    c = ctx()
    user = c.require_user()
    try:
        path = preview_mod.generate(c.conn, c.settings, lead_id, user.id)
        console.print(f"Preview saved: {path}")
        if publish:
            url = preview_mod.publish(c.conn, c.settings, c.clients, lead_id, user.id)
            console.print(f"Published: {url}")
    except LeadError as e:
        fail(str(e))


@dnc_app.command("add")
def dnc_add(phone: str, reason: Optional[str] = typer.Option(None, "--reason"),
            no_email: bool = typer.Option(False, "--no-email", help="Also suppress the lead's email")) -> None:
    """Add a number to the Do Not Call list."""
    c = ctx()
    user = c.require_user()
    email = None
    if no_email:
        row = c.conn.execute("SELECT email FROM leads WHERE phone_e164=? AND email IS NOT NULL", (phones.normalise_e164(phone),)).fetchone()
        email = row["email"] if row else None
    try:
        e164 = leads_mod.dnc_add(c.conn, phone, reason, user.id, also_email=email)
    except LeadError as e:
        fail(str(e))
    console.print(f"{e164} added to Do Not Call." + (f" {email} suppressed." if email else ""))


@dnc_app.command("remove")
def dnc_remove(phone: str, yes: bool = typer.Option(False, "--yes", help="Skip the confirmation")) -> None:
    """Remove a number from the Do Not Call list."""
    c = ctx()
    user = c.require_user()
    if not yes and not typer.confirm(f"Remove {phone} from Do Not Call? They asked not to be called.", default=False):
        console.print("Cancelled.")
        return
    ok = leads_mod.dnc_remove(c.conn, phone, user.id)
    console.print("Removed." if ok else "That number isn't on the list.")


@dnc_app.command("list")
def dnc_list() -> None:
    """Show the Do Not Call list."""
    c = ctx()
    t = Table(header_style="bold", safe_box=True)
    for col in ("Number", "Business", "Reason", "Added", "By"):
        t.add_column(col)
    for r in leads_mod.dnc_list(c.conn):
        t.add_row(r["phone_e164"], r.get("lead_name") or "", r.get("reason") or "", fmt_local(r["created_at"], False), r.get("initials") or "")
    console.print(t)


@backup_app.callback()
def backup_main(ctx_: typer.Context) -> None:
    """Back up now (no subcommand), or list / restore."""
    if ctx_.invoked_subcommand is None:
        c = ctx()
        b = backup_mod.make_backup(c.conn, c.settings)
        console.print(f"Backup saved: {b.path}")
        if c.settings.backup_dir_is_default:
            console.print("[yellow]BACKUP_DIR isn't set; backups are in ./backups on this PC only.[/]")


@backup_app.command("list")
def backup_list() -> None:
    """List backups."""
    c = ctx()
    t = Table(header_style="bold", safe_box=True)
    for col in ("File", "Taken", "Size"):
        t.add_column(col)
    for b in backup_mod.list_backups(c.settings):
        t.add_row(b.name, fmt_local(b.taken_at), f"{b.size / 1024:.0f} KB")
    console.print(t)


@backup_app.command("restore")
def backup_restore(name: str) -> None:
    """Restore a backup (asks you to type RESTORE; takes a safety backup first)."""
    c = ctx()
    c.require_user()
    word = typer.prompt("This replaces the current database. Type RESTORE to continue")
    try:
        safety = backup_mod.restore(c.conn, c.settings, name, word)
    except (ValueError, FileNotFoundError) as e:
        fail(str(e))
    console.print(f"Restored {name}. The previous database was saved as {safety.name}.")


# ---------------------------------------------------------------------------
# Trades and the dashboard
# ---------------------------------------------------------------------------

@app.command()
def trades() -> None:
    """List the trades in trades.yaml."""
    c = ctx()
    for key, t in load_trades(c.settings).items():
        console.print(f"{t.label:<24} [dim]{key}  synonyms: {', '.join(t.synonyms)}[/]")


@app.command()
def ui(port: int = typer.Option(8765, "--port"), no_browser: bool = typer.Option(False, "--no-browser")) -> None:
    """Start the dashboard and open it in your browser."""
    import uvicorn

    c = ctx()
    if users.user_count(c.conn) == 0:
        fail('No accounts yet. Create one for each person first, e.g.:\n'
             '  leadfinder user add "Edison Young"\n  leadfinder user add "Jack Alcock"')
    host = c.settings.bind_host or "127.0.0.1"
    if host == "0.0.0.0":
        fail("BIND_HOST=0.0.0.0 would expose the dashboard to every network. Use 127.0.0.1 or your Tailscale IP.")
    from .web.app import create_app

    application = create_app(c.settings)
    url = f"http://{'localhost' if host == '127.0.0.1' else host}:{port}"
    console.print(f"Dashboard running at [bold]{url}[/]  (Ctrl+C to stop)")
    if not no_browser:
        webbrowser.open(url)
    uvicorn.run(application, host=host, port=port, log_level="warning")


