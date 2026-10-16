from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from leadfinder import backup, calls, preview, stats, timeutil, users

from .conftest import TEST_PASSWORD
from .helpers import add_lead


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

def test_backup_once_per_day(conn, settings):
    first = backup.ensure_daily_backup(conn, settings)
    assert first is not None and first.path.exists()
    assert backup.ensure_daily_backup(conn, settings) is None  # same day: nothing
    timeutil.set_now(timeutil.now_utc() + timedelta(days=1))
    assert backup.ensure_daily_backup(conn, settings) is not None
    assert len(backup.list_backups(settings)) == 2


def test_backup_is_a_valid_sqlite_copy(conn, settings, edison):
    add_lead(conn, name="Backed Up Ltd")
    b = backup.make_backup(conn, settings)
    c2 = sqlite3.connect(str(b.path))
    assert c2.execute("SELECT name FROM leads").fetchone()[0] == "Backed Up Ltd"
    c2.close()


def test_retention_keeps_30_daily_and_12_monthly(conn, settings):
    start = datetime(2025, 6, 1, 9, 0, tzinfo=timeutil.UTC)
    for d in range(0, 500, 1):  # one backup a day for ~16 months, two on some days
        timeutil.set_now(start + timedelta(days=d))
        backup.make_backup(conn, settings)
        if d % 50 == 0:
            timeutil.set_now(start + timedelta(days=d, hours=2))
            backup.make_backup(conn, settings)
    items = backup.list_backups(settings)
    days = {b.taken_at.astimezone(timeutil.LONDON).date() for b in items}
    months = {b.taken_at.astimezone(timeutil.LONDON).strftime("%Y-%m") for b in items}
    latest = max(days)
    recent = [d for d in days if (latest - d).days < 30]
    assert len(recent) == 30
    assert len(months) == 12
    assert len(items) <= 30 + 12
    assert len(items) == len(days)  # one per day kept


def test_restore_takes_safety_backup_first(conn, settings, edison):
    add_lead(conn, name="Original")
    b = backup.make_backup(conn, settings)
    add_lead(conn, name="Added later")
    with pytest.raises(ValueError):
        backup.restore(conn, settings, b.name, "restore")
    timeutil.set_now(timeutil.now_utc() + timedelta(seconds=5))
    safety = backup.restore(conn, settings, b.name, "RESTORE")
    assert "pre-restore" in safety.name
    names = {r[0] for r in conn.execute("SELECT name FROM leads")}
    assert names == {"Original"}
    c2 = sqlite3.connect(str(safety.path))
    assert {r[0] for r in c2.execute("SELECT name FROM leads")} == {"Original", "Added later"}
    c2.close()


def test_restore_rejects_paths_outside_folder(conn, settings):
    with pytest.raises(FileNotFoundError):
        backup.restore(conn, settings, "../../leads.db", "RESTORE")


# ---------------------------------------------------------------------------
# Previews
# ---------------------------------------------------------------------------

def test_preview_html(conn, settings, edison):
    lid = add_lead(conn, name="Smith & Sons Plumbing", trade="plumbers", town="Crawley", phone="01293 555123",
                   review_count=8137, rating=4.3)
    conn.execute("UPDATE leads SET ai_verdict='Google review text must never appear' WHERE id=?", (lid,))
    conn.commit()
    path = preview.generate(conn, settings, lid, edison.id)
    html = path.read_text(encoding="utf-8")
    assert path.parent.parent == settings.previews_dir
    assert html.lower().startswith("<!doctype html>") and html.rstrip().endswith("</html>")
    assert "Smith &amp; Sons Plumbing" in html
    assert 'href="tel:+441293555123"' in html and "Call now" in html
    assert '<meta name="robots" content="noindex' in html
    assert "Concept design by Vibora" in html
    for forbidden in ("googleusercontent", "maps.google", "review", "Google review text", "4.3", "8137"):
        assert forbidden not in html
    for section in ('id="services"', "Why choose", "Areas we cover", 'id="contact"'):
        assert section in html
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []
            self.ok = True

        def handle_starttag(self, tag, attrs):
            if tag not in ("meta", "link", "br", "img", "input"):
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if not self.stack or self.stack.pop() != tag:
                self.ok = False

    p = P()
    p.feed(html)
    assert p.ok and p.stack == []


@pytest.mark.parametrize("trade,family", [("barbers", "beauty"), ("cafes", "food"), ("accountants", "professional"),
                                          ("saddlers", "generic"), ("plumbers", "trades")])
def test_preview_families(conn, settings, edison, trade, family):
    lid = add_lead(conn, name="Test Biz", trade=trade)
    html = preview.generate(conn, settings, lid, edison.id).read_text(encoding="utf-8")
    assert "Test Biz" in html and "Concept design by Vibora" in html


def test_preview_publish_and_export(conn, settings, edison):
    from .fakes import FakePublisher, fake_clients

    lid = add_lead(conn, name="Publish Me")
    pub = FakePublisher()
    url = preview.publish(conn, settings, fake_clients(publisher=pub), lid, edison.id)
    assert url.startswith("https://") and conn.execute("SELECT preview_url FROM leads WHERE id=?", (lid,)).fetchone()[0] == url
    name, data = preview.export_zip(conn, settings, lid)
    assert name.endswith(".zip") and data[:2] == b"PK"


# ---------------------------------------------------------------------------
# Streaks, leaderboard, passwords
# ---------------------------------------------------------------------------

def test_streaks():
    t = date(2026, 10, 16)
    days = {t, t - timedelta(days=1), t - timedelta(days=2), t - timedelta(days=10), t - timedelta(days=11),
            t - timedelta(days=12), t - timedelta(days=13), t - timedelta(days=14)}
    assert stats.streaks(days, t) == (3, 5)
    # Today not done yet: the streak still counts up to yesterday
    assert stats.streaks(days - {t}, t) == (2, 5)
    assert stats.streaks(set(), t) == (0, 0)
    assert stats.streaks({t - timedelta(days=3)}, t) == (0, 1)


def test_grafting_counts_manual_entries(conn, settings, edison):
    calls.add_manual_entry(conn, edison.id, "Door-knocking", 5, 60, entry_date="2026-10-15")
    calls.log_call(conn, settings, add_lead(conn), edison.id, "no_answer")
    g = stats.grafting(conn, edison.id)
    assert g["current_streak"] == 2 and g["total"] == 6


def test_leaderboard_maths(conn, settings, edison, jack):
    a, b = add_lead(conn), add_lead(conn)
    for o, d in [("no_answer", 10), ("connected", 120), ("meeting_booked", 300), ("not_interested", 60)]:
        calls.log_call(conn, settings, a, edison.id, o, duration_seconds=d,
                       meeting_at=timeutil.now_utc() + timedelta(days=1) if o == "meeting_booked" else None)
    calls.log_call(conn, settings, b, jack.id, "no_answer", duration_seconds=5)
    calls.set_stage(conn, a, edison.id, "won", setup_fee=1000, retainer=99)
    rows = {r["name"]: r for r in stats.leaderboard(conn, "today")}
    ed = rows["Edison Young"]
    assert (ed["dials"], ed["connects"], ed["meetings"], ed["deals"]) == (4, 3, 1, 1)
    assert ed["connect_rate"] == 75.0 and ed["revenue"] == 1000 and ed["talk_seconds"] == 490
    assert ed["leader"] and not rows["Jack Alcock"]["leader"]
    k = stats.kpis(conn, "today", None)
    assert k["dials"] == 5 and k["mrr"] == 99 and k["calls_today"] == 5 and k["target"] == 80
    k_ed = stats.kpis(conn, "today", [edison.id])
    assert k_ed["dials"] == 4 and abs(k_ed["dial_meeting_rate"] - 25.0) < 1e-9


def test_no_plaintext_password_in_database(conn, settings, edison):
    raw = settings.db_path.read_bytes()
    conn.commit()
    for p in (settings.db_path, settings.db_path.with_name("leads.db-wal")):
        if p.exists():
            assert TEST_PASSWORD.encode() not in p.read_bytes()
    h = conn.execute("SELECT password_hash FROM users WHERE id=?", (edison.id,)).fetchone()[0]
    assert h.startswith("$argon2") and TEST_PASSWORD not in h
    _ = raw


def test_password_rules(conn):
    with pytest.raises(users.AuthError):
        users.add_user(conn, "Short Pw", "1234567")
    u = users.add_user(conn, "Anna  Bell", "longenough")
    assert u.full_name == "Anna Bell" and u.initials == "AB"
    with pytest.raises(users.AuthError):
        users.add_user(conn, "anna bell", "longenough")
    assert users.authenticate(conn, " ANNA   bell ", "longenough").id == u.id


def test_cli_login_file_is_signed(conn, settings, edison):
    users.cli_login(settings, edison)
    assert users.cli_current_user(conn, settings).id == edison.id
    text = settings.cli_state_path.read_text(encoding="utf-8")
    assert TEST_PASSWORD not in text
    settings.cli_state_path.write_text(text.replace('"token": "', '"token": "x'), encoding="utf-8")
    assert users.cli_current_user(conn, settings) is None


def test_secret_key_generated_on_first_run(tmp_path):
    from leadfinder.config import load_settings

    s = load_settings(tmp_path)
    assert len(s.secret_key) > 40
    assert f"SECRET_KEY={s.secret_key}" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert load_settings(tmp_path).secret_key == s.secret_key
    assert "secret" not in repr(s).lower() or s.secret_key not in repr(s)


def test_refresh_stays_in_allowance(conn, settings, edison):
    from leadfinder.freetier import PLACE_DETAILS
    from leadfinder.search import refresh_leads

    from .fakes import FakePlaces, fake_clients, place

    old = timeutil.to_db(timeutil.now_utc() - timedelta(days=40))
    ids = [add_lead(conn, place_id=f"r{i}", last_refreshed=old, status=s) for i, s in enumerate(["new", "won", "called", "dnc"])]
    conn.executemany("INSERT INTO api_usage(sku, ts) VALUES (?, ?)", [(PLACE_DETAILS, "2026-10-02 00:00:00")] * 899)
    conn.commit()
    places = FakePlaces(details={f"r{i}": place(f"r{i}", f"Fresh {i}", "01293 444555") for i in range(4)})
    rep = refresh_leads(conn, settings, fake_clients(places=places), edison)
    assert places.detail_calls == ["r0"]  # active leads only, stopped at the limit
    assert rep.refreshed == 1 and "Place Details" in rep.stopped
    _ = ids
