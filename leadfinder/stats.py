"""Numbers for `leadfinder stats`, the Dashboard, the Leaderboard and My grafting."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any

from . import db
from .calls import CONNECT_OUTCOMES, OUTCOMES
from .timeutil import LONDON, from_db, london_day_bounds, to_db, today_london

PERIODS = {"today": "Today", "week": "This week", "month": "This month", "all": "All time"}
INTERESTED_OUTCOMES = ("connected", "meeting_booked")
DEFAULT_DEAL = 800.0


def _london_midnight(d: date) -> datetime:
    return datetime.combine(d, time(0, 0), tzinfo=LONDON)


def period_bounds(period: str, today: date | None = None) -> dict[str, str | None]:
    """UTC DB strings: start/end of the period and of the previous period of the same length."""
    t = today or today_london()
    if period == "today":
        s, e = t, t + timedelta(days=1)
        ps, pe = t - timedelta(days=1), t
    elif period == "week":
        s = t - timedelta(days=t.weekday())
        e = s + timedelta(days=7)
        ps, pe = s - timedelta(days=7), s
    elif period == "month":
        s = t.replace(day=1)
        e = (s + timedelta(days=32)).replace(day=1)
        pe = s
        ps = (s - timedelta(days=1)).replace(day=1)
    else:
        return {"start": None, "end": None, "prev_start": None, "prev_end": None}
    f = lambda d: to_db(_london_midnight(d))  # noqa: E731
    return {"start": f(s), "end": f(e), "prev_start": f(ps), "prev_end": f(pe)}


def _where(col: str, start: str | None, end: str | None, user_col: str | None, user_ids: list[int] | None) -> tuple[str, list[Any]]:
    parts, args = ["1=1"], []
    if start:
        parts.append(f"{col} >= ?")
        args.append(start)
    if end:
        parts.append(f"{col} < ?")
        args.append(end)
    if user_col and user_ids:
        parts.append(f"{user_col} IN ({','.join('?' * len(user_ids))})")
        args += user_ids
    return " AND ".join(parts), args


def _counts(conn: sqlite3.Connection, start: str | None, end: str | None, user_ids: list[int] | None) -> dict[str, float]:
    w, a = _where("created_at", start, end, "user_id", user_ids)
    dials = conn.execute(f"SELECT COUNT(*) FROM calls WHERE {w}", a).fetchone()[0]
    connects = conn.execute(
        f"SELECT COUNT(*) FROM calls WHERE {w} AND outcome IN ({','.join('?' * len(CONNECT_OUTCOMES))})",
        a + list(CONNECT_OUTCOMES),
    ).fetchone()[0]
    meetings = conn.execute(f"SELECT COUNT(*) FROM calls WHERE {w} AND outcome='meeting_booked'", a).fetchone()[0]
    talk = conn.execute(f"SELECT COALESCE(SUM(duration_seconds),0) FROM calls WHERE {w}", a).fetchone()[0]
    ws, sa = _where("created_at", start, end, "user_id", user_ids)
    won = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(setup_fee),0) FROM stage_changes WHERE {ws} AND to_stage='won'", sa).fetchone()
    wm, ma = _where("start_at", start, end, "user_id", user_ids)
    held = conn.execute(f"SELECT COUNT(*) FROM meetings WHERE {wm} AND status='held'", ma).fetchone()[0]
    return {
        "dials": dials,
        "connects": connects,
        "meetings": meetings,
        "deals": won[0],
        "revenue": float(won[1] or 0),
        "held": held,
        "talk_seconds": talk,
    }


def _rate(a: float, b: float) -> float | None:
    return None if not b else 100.0 * a / b


def mrr(conn: sqlite3.Connection, user_ids: list[int] | None = None) -> float:
    sql = "SELECT COALESCE(SUM(retainer),0) FROM leads WHERE status='won'"
    args: list[Any] = []
    if user_ids:
        sql += f" AND owner_id IN ({','.join('?' * len(user_ids))})"
        args += user_ids
    return float(conn.execute(sql, args).fetchone()[0])


def average_deal(conn: sqlite3.Connection) -> float:
    v = conn.execute("SELECT AVG(setup_fee) FROM leads WHERE status='won' AND setup_fee > 0").fetchone()[0]
    if v:
        return float(v)
    return float(db.get_setting(conn, "default_deal_value", DEFAULT_DEAL) or DEFAULT_DEAL)


def pipeline_value(conn: sqlite3.Connection, user_ids: list[int] | None = None) -> float:
    sql = "SELECT COUNT(*) FROM leads WHERE status IN ('meeting_booked','proposal_sent')"
    args: list[Any] = []
    if user_ids:
        sql += f" AND owner_id IN ({','.join('?' * len(user_ids))})"
        args += user_ids
    n = conn.execute(sql, args).fetchone()[0]
    return n * average_deal(conn)


def daily_series(conn: sqlite3.Connection, user_ids: list[int] | None, days: int = 14) -> dict[str, list[int]]:
    t = today_london()
    out: dict[str, list[int]] = {"dials": [], "connects": [], "meetings": [], "deals": []}
    for i in range(days - 1, -1, -1):
        d = t - timedelta(days=i)
        s, e = london_day_bounds(d)
        c = _counts(conn, s, e, user_ids)
        for k in out:
            out[k].append(int(c[k]))
    return out


def daily_target(conn: sqlite3.Connection, user_ids: list[int] | None) -> int:
    sql = "SELECT COALESCE(SUM(daily_call_target),0) FROM users"
    args: list[Any] = []
    if user_ids:
        sql += f" WHERE id IN ({','.join('?' * len(user_ids))})"
        args += user_ids
    return int(conn.execute(sql, args).fetchone()[0]) or 40


def kpis(conn: sqlite3.Connection, period: str, user_ids: list[int] | None) -> dict[str, Any]:
    b = period_bounds(period)
    cur = _counts(conn, b["start"], b["end"], user_ids)
    prev = _counts(conn, b["prev_start"], b["prev_end"], user_ids) if b["prev_start"] else None
    s, e = london_day_bounds(today_london())
    today = _counts(conn, s, e, user_ids)

    def change(key: str, rate_of: tuple[str, str] | None = None) -> float | None:
        if prev is None:
            return None
        if rate_of:
            a, c = _rate(cur[rate_of[0]], cur[rate_of[1]]), _rate(prev[rate_of[0]], prev[rate_of[1]])
            return None if a is None or c is None else a - c
        return cur[key] - prev[key]

    return {
        "period": period,
        "calls_today": today["dials"],
        "target": daily_target(conn, user_ids),
        "dials": cur["dials"],
        "connects": cur["connects"],
        "connect_rate": _rate(cur["connects"], cur["dials"]),
        "connect_rate_change": change("", ("connects", "dials")),
        "meetings": cur["meetings"],
        "meetings_change": change("meetings"),
        "deals": cur["deals"],
        "deals_change": change("deals"),
        "close_rate": _rate(cur["deals"], cur["held"]),
        "close_rate_change": change("", ("deals", "held")),
        "dial_meeting_rate": _rate(cur["meetings"], cur["dials"]),
        "dial_meeting_rate_change": change("", ("meetings", "dials")),
        "revenue": cur["revenue"],
        "revenue_change": change("revenue"),
        "mrr": mrr(conn, user_ids),
        "pipeline_value": pipeline_value(conn, user_ids),
        "dials_change": change("dials"),
        "series": daily_series(conn, user_ids),
    }


def heatmap(conn: sqlite3.Connection, period: str, user_ids: list[int] | None) -> dict[str, Any]:
    """Day x hour connect rate from logged calls (London time, 8am-8pm)."""
    b = period_bounds(period)
    w, a = _where("created_at", b["start"], b["end"], "user_id", user_ids)
    hours = list(range(8, 21))
    grid = [[{"dials": 0, "connects": 0} for _ in hours] for _ in range(7)]
    for r in conn.execute(f"SELECT created_at, outcome FROM calls WHERE {w}", a).fetchall():
        loc = from_db(r["created_at"]).astimezone(LONDON)  # type: ignore[union-attr]
        if loc.hour not in hours:
            continue
        cell = grid[loc.weekday()][hours.index(loc.hour)]
        cell["dials"] += 1
        if r["outcome"] in CONNECT_OUTCOMES:
            cell["connects"] += 1
    for row in grid:
        for c in row:
            c["rate"] = _rate(c["connects"], c["dials"])
    return {"hours": hours, "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "grid": grid}


def outcome_breakdown(conn: sqlite3.Connection, period: str | None, user_ids: list[int] | None) -> list[dict[str, Any]]:
    b = period_bounds(period or "all")
    w, a = _where("created_at", b["start"], b["end"], "user_id", user_ids)
    rows = {r["outcome"]: r["n"] for r in conn.execute(
        f"SELECT outcome, COUNT(*) AS n FROM calls WHERE {w} GROUP BY outcome", a
    ).fetchall()}
    labels = dict(OUTCOMES, dnc="Do not call")
    return [{"outcome": k, "label": labels[k], "n": rows.get(k, 0)} for k in labels]


def performance(conn: sqlite3.Connection, period: str, user_ids: list[int] | None, by: str = "trade") -> list[dict[str, Any]]:
    assert by in ("trade", "town")
    b = period_bounds(period)
    w, a = _where("c.created_at", b["start"], b["end"], "c.user_id", user_ids)
    rows = conn.execute(
        f"""SELECT l.{by} AS k, COUNT(*) AS dials,
                   SUM(CASE WHEN c.outcome IN ({','.join('?' * len(CONNECT_OUTCOMES))}) THEN 1 ELSE 0 END) AS connects,
                   SUM(CASE WHEN c.outcome='meeting_booked' THEN 1 ELSE 0 END) AS meetings
            FROM calls c JOIN leads l ON l.id=c.lead_id WHERE {w} GROUP BY l.{by}""",
        list(CONNECT_OUTCOMES) + a,
    ).fetchall()
    ws, sa = _where("s.created_at", b["start"], b["end"], "s.user_id", user_ids)
    won = {r["k"]: r["n"] for r in conn.execute(
        f"SELECT l.{by} AS k, COUNT(*) AS n FROM stage_changes s JOIN leads l ON l.id=s.lead_id WHERE {ws} AND s.to_stage='won' GROUP BY l.{by}",
        sa,
    ).fetchall()}
    out = []
    for r in rows:
        d = dict(r)
        d["won"] = won.get(r["k"], 0)
        d["connect_rate"] = _rate(d["connects"], d["dials"])
        d["meeting_rate"] = _rate(d["meetings"], d["dials"])
        d["close_rate"] = _rate(d["won"], d["meetings"])
        out.append(d)
    return sorted(out, key=lambda d: (-(d["meeting_rate"] or 0), -(d["close_rate"] or 0), -d["dials"]))


def funnel(conn: sqlite3.Connection, period: str, user_ids: list[int] | None) -> list[dict[str, Any]]:
    b = period_bounds(period)
    wl, al = _where("first_seen", b["start"], b["end"], "owner_id", user_ids)
    leads = conn.execute(f"SELECT COUNT(*) FROM leads WHERE duplicate_of IS NULL AND status != 'duplicate' AND {wl}", al).fetchone()[0]
    w, a = _where("created_at", b["start"], b["end"], "user_id", user_ids)
    dialled = conn.execute(f"SELECT COUNT(DISTINCT lead_id) FROM calls WHERE {w}", a).fetchone()[0]
    connected = conn.execute(
        f"SELECT COUNT(DISTINCT lead_id) FROM calls WHERE {w} AND outcome IN ({','.join('?' * len(CONNECT_OUTCOMES))})",
        a + list(CONNECT_OUTCOMES),
    ).fetchone()[0]
    meeting = conn.execute(f"SELECT COUNT(DISTINCT lead_id) FROM calls WHERE {w} AND outcome='meeting_booked'", a).fetchone()[0]
    proposal = conn.execute(f"SELECT COUNT(DISTINCT lead_id) FROM stage_changes WHERE {w} AND to_stage='proposal_sent'", a).fetchone()[0]
    won = conn.execute(f"SELECT COUNT(DISTINCT lead_id) FROM stage_changes WHERE {w} AND to_stage='won'", a).fetchone()[0]
    stages = [("Leads", leads), ("Dialled", dialled), ("Connected", connected), ("Meeting", meeting), ("Proposal", proposal), ("Won", won)]
    out = []
    for i, (label, n) in enumerate(stages):
        conv = None if i == 0 else _rate(n, stages[i - 1][1])
        out.append({"label": label, "n": n, "conv": conv})
    return out


def activity_feed(conn: sqlite3.Connection, user_ids: list[int] | None, limit: int = 20) -> list[dict[str, Any]]:
    sql = """SELECT a.*, u.initials, u.full_name, l.name AS lead_name FROM activity a
             LEFT JOIN users u ON u.id=a.user_id LEFT JOIN leads l ON l.id=a.lead_id
             WHERE a.kind IN ('call','stage','email_sent','search','preview_publish','dnc','manual','meeting')"""
    args: list[Any] = []
    if user_ids:
        sql += f" AND a.user_id IN ({','.join('?' * len(user_ids))})"
        args += user_ids
    sql += " ORDER BY a.id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def recent_items(conn: sqlite3.Connection, user_ids: list[int] | None, limit: int = 15) -> dict[str, list[dict[str, Any]]]:
    """Sidebar 'Recent': one line per lead touched, grouped Today / Yesterday / Earlier."""
    sql = """SELECT a.lead_id, MAX(a.id) AS aid FROM activity a WHERE a.lead_id IS NOT NULL"""
    args: list[Any] = []
    if user_ids:
        sql += f" AND a.user_id IN ({','.join('?' * len(user_ids))})"
        args += user_ids
    sql += " GROUP BY a.lead_id ORDER BY aid DESC LIMIT ?"
    args.append(limit)
    ids = [r["aid"] for r in conn.execute(sql, args).fetchall()]
    groups: dict[str, list[dict[str, Any]]] = {"Today": [], "Yesterday": [], "Earlier": []}
    if not ids:
        return groups
    rows = conn.execute(
        f"""SELECT a.*, l.name AS lead_name, u.initials FROM activity a JOIN leads l ON l.id=a.lead_id
            LEFT JOIN users u ON u.id=a.user_id WHERE a.id IN ({','.join('?' * len(ids))}) ORDER BY a.id DESC""",
        ids,
    ).fetchall()
    t = today_london()
    for r in rows:
        d = from_db(r["created_at"]).astimezone(LONDON).date()  # type: ignore[union-attr]
        key = "Today" if d == t else "Yesterday" if d == t - timedelta(days=1) else "Earlier"
        groups[key].append(dict(r))
    return groups


def leaderboard(conn: sqlite3.Connection, period: str) -> list[dict[str, Any]]:
    b = period_bounds(period)
    out = []
    for u in conn.execute("SELECT id, full_name, initials FROM users ORDER BY id").fetchall():
        c = _counts(conn, b["start"], b["end"], [u["id"]])
        out.append({
            "user_id": u["id"],
            "name": u["full_name"],
            "initials": u["initials"],
            "dials": c["dials"],
            "connects": c["connects"],
            "connect_rate": _rate(c["connects"], c["dials"]),
            "meetings": c["meetings"],
            "deals": c["deals"],
            "revenue": c["revenue"],
            "talk_seconds": c["talk_seconds"],
        })
    if out:
        best = max(out, key=lambda r: (r["deals"], r["meetings"], r["dials"]))
        for r in out:
            r["leader"] = r is best and (best["dials"] or best["deals"])
    return out


def today_strip(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    s, e = london_day_bounds(today_london())
    out = []
    for u in conn.execute("SELECT id, full_name, initials, daily_call_target FROM users ORDER BY id").fetchall():
        dials = conn.execute("SELECT COUNT(*) FROM calls WHERE user_id=? AND created_at>=? AND created_at<?", (u["id"], s, e)).fetchone()[0]
        out.append({"user_id": u["id"], "name": u["full_name"], "initials": u["initials"], "dials": dials, "target": u["daily_call_target"]})
    return out


# ---------------------------------------------------------------------------
# CLI stats
# ---------------------------------------------------------------------------

def cli_stats(conn: sqlite3.Connection, user_ids: list[int] | None = None) -> dict[str, Any]:
    today = _counts(conn, *london_day_bounds(today_london()), user_ids)
    wb = period_bounds("week")
    week = _counts(conn, wb["start"], wb["end"], user_ids)
    w, a = _where("created_at", None, None, "user_id", user_ids)
    dialled = conn.execute(f"SELECT COUNT(DISTINCT lead_id) FROM calls WHERE {w}", a).fetchone()[0]
    interested = conn.execute(
        f"SELECT COUNT(DISTINCT lead_id) FROM calls WHERE {w} AND outcome IN ({','.join('?' * len(INTERESTED_OUTCOMES))})",
        a + list(INTERESTED_OUTCOMES),
    ).fetchone()[0]
    return {
        "today": today,
        "week": week,
        "outcomes": outcome_breakdown(conn, "all", user_ids),
        "dialled_leads": dialled,
        "interested_leads": interested,
        "conversion": _rate(interested, dialled),
    }


# ---------------------------------------------------------------------------
# My grafting
# ---------------------------------------------------------------------------

def activity_by_day(conn: sqlite3.Connection, user_id: int, days: int = 365) -> dict[date, int]:
    t = today_london()
    start = t - timedelta(days=days - 1)
    s = to_db(_london_midnight(start))
    counts: dict[date, int] = defaultdict(int)
    for r in conn.execute("SELECT created_at FROM calls WHERE user_id=? AND created_at>=?", (user_id, s)).fetchall():
        counts[from_db(r["created_at"]).astimezone(LONDON).date()] += 1  # type: ignore[union-attr]
    for r in conn.execute(
        "SELECT entry_date, SUM(count) AS n FROM manual_entries WHERE user_id=? AND entry_date>=? GROUP BY entry_date",
        (user_id, start.isoformat()),
    ).fetchall():
        counts[date.fromisoformat(r["entry_date"])] += int(r["n"])
    return dict(counts)


def streaks(active_days: set[date], today: date | None = None) -> tuple[int, int]:
    """(current, longest). The current streak may end today or yesterday (today isn't over)."""
    t = today or today_london()
    longest = run = 0
    prev: date | None = None
    for d in sorted(active_days):
        run = run + 1 if prev and d - prev == timedelta(days=1) else 1
        longest = max(longest, run)
        prev = d
    cur = 0
    d = t if t in active_days else t - timedelta(days=1)
    while d in active_days:
        cur += 1
        d -= timedelta(days=1)
    return cur, longest


def contribution_grid(counts: dict[date, int], days: int = 365) -> list[list[dict[str, Any]]]:
    """Weeks (columns) x weekdays (rows), GitHub style, oldest first."""
    t = today_london()
    start = t - timedelta(days=days - 1)
    start -= timedelta(days=start.weekday())  # Monday
    mx = max(counts.values()) if counts else 0
    weeks: list[list[dict[str, Any]]] = []
    d = start
    while d <= t:
        week = []
        for _ in range(7):
            n = counts.get(d, 0)
            level = 0 if n == 0 or mx == 0 else min(4, 1 + int(3 * n / mx))
            week.append({"date": d.isoformat(), "n": n, "level": level, "future": d > t})
            d += timedelta(days=1)
        weeks.append(week)
    return weeks


def grafting(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    counts = activity_by_day(conn, user_id)
    cur, longest = streaks({d for d, n in counts.items() if n > 0})
    sessions = [dict(r) for r in conn.execute(
        "SELECT * FROM dial_sessions WHERE user_id=? ORDER BY id DESC LIMIT 50", (user_id,)
    ).fetchall()]
    for s in sessions:
        st, en = from_db(s["started_at"]), from_db(s["ended_at"])
        s["seconds"] = int((en - st).total_seconds()) if st and en else 0
    t = today_london()
    week_start = t - timedelta(days=t.weekday())
    hours_today = sum(s["seconds"] for s in sessions if from_db(s["started_at"]).astimezone(LONDON).date() == t) / 3600  # type: ignore[union-attr]
    hours_week = sum(s["seconds"] for s in sessions if from_db(s["started_at"]).astimezone(LONDON).date() >= week_start) / 3600  # type: ignore[union-attr]

    # Personal records
    per_day: dict[date, int] = defaultdict(int)
    meetings_week: dict[tuple[int, int], int] = defaultdict(int)
    for r in conn.execute("SELECT created_at, outcome FROM calls WHERE user_id=?", (user_id,)).fetchall():
        d = from_db(r["created_at"]).astimezone(LONDON).date()  # type: ignore[union-attr]
        per_day[d] += 1
        if r["outcome"] == "meeting_booked":
            meetings_week[tuple(d.isocalendar()[:2])] += 1  # type: ignore[index]
    best_day = max(per_day.items(), key=lambda kv: kv[1]) if per_day else None
    best_week = max(meetings_week.items(), key=lambda kv: kv[1]) if meetings_week else None
    best_session = conn.execute(
        "SELECT *, 100.0*connects/dials AS rate FROM dial_sessions WHERE user_id=? AND dials >= 20 ORDER BY rate DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    manual = [dict(r) for r in conn.execute(
        "SELECT * FROM manual_entries WHERE user_id=? ORDER BY entry_date DESC, id DESC LIMIT 30", (user_id,)
    ).fetchall()]
    return {
        "grid": contribution_grid(counts),
        "total": sum(counts.values()),
        "current_streak": cur,
        "longest_streak": longest,
        "sessions": sessions,
        "hours_today": hours_today,
        "hours_week": hours_week,
        "record_day": {"date": best_day[0].isoformat(), "n": best_day[1]} if best_day else None,
        "record_week": {"week": f"{best_week[0][0]}-W{best_week[0][1]:02d}", "n": best_week[1]} if best_week else None,
        "record_session": dict(best_session) if best_session else None,
        "manual": manual,
    }


