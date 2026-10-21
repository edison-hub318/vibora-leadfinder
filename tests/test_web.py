from __future__ import annotations

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from leadfinder import calls, leads, timeutil
from leadfinder.web.app import create_app

from .conftest import TEST_PASSWORD
from .fakes import fake_clients
from .helpers import add_lead


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf-token" content="([^"]+)"', html) or re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


def login(client: TestClient, name: str = "Edison Young", password: str = TEST_PASSWORD, remember: bool = False):
    page = client.get("/login")
    token = csrf_of(page.text)
    data = {"full_name": name, "password": password, "csrf_token": token}
    if remember:
        data["remember"] = "1"
    return client.post("/login", data=data, follow_redirects=False)


@pytest.fixture
def app(settings, conn, edison, jack):
    from .fakes import FakePlaces, crawley_plumbers

    places = FakePlaces(pages={"plumbers in Crawley, UK": [crawley_plumbers()]})
    return create_app(settings, clients_factory=lambda s: fake_clients(places=places))


@pytest.fixture
def client(app):
    c = TestClient(app)
    r = login(c)
    assert r.status_code == 303
    return c


def token(client: TestClient) -> str:
    return csrf_of(client.get("/settings").text)


PAGES = ["/", "/?period=week&who=team", "/?period=all", "/dial", "/leads", "/pipeline", "/meetings", "/outreach",
         "/previews", "/objections", "/dnc", "/backups", "/settings", "/grafting", "/partials/recent?scope=team"]


def test_every_page_returns_200(client, conn, settings, edison):
    lid = add_lead(conn, name="Smith Plumbing")
    calls.log_call(conn, settings, lid, edison.id, "meeting_booked", meeting_at=timeutil.now_utc() + timedelta(days=2))
    calls.log_call(conn, settings, add_lead(conn), edison.id, "not_interested", objections=["too_expensive"])
    for p in PAGES:
        r = client.get(p)
        assert r.status_code == 200, (p, r.text[:500])
    assert client.get(f"/leads/{lid}/panel").status_code == 200
    assert client.get("/leads/table?q=Smith").status_code == 200


def test_unauthenticated_redirected_to_login(app):
    c = TestClient(app)
    for p in ["/", "/dial", "/leads", "/settings"]:
        r = c.get(p, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    r = c.get("/dial/next", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"
    assert c.get("/login").status_code == 200
    assert c.get("/static/app.css").status_code == 200


def test_login_success_and_failure(app):
    c = TestClient(app)
    r = login(c, "  edison   YOUNG ")
    assert r.status_code == 303
    assert c.get("/", follow_redirects=False).status_code == 200
    c2 = TestClient(app)
    bad = login(c2, "Edison Young", "wrong-password")
    assert bad.status_code == 401 and "Name or password incorrect" in bad.text
    nouser = login(c2, "Nobody Here", "wrong-password")
    assert "Name or password incorrect" in nouser.text


def test_session_cookie_flags(app):
    c = TestClient(app)
    r = login(c)
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert "Max-Age" not in cookie  # 12h session cookie unless "keep me logged in"
    c2 = TestClient(app)
    r2 = login(c2, remember=True)
    assert "Max-Age=2592000" in r2.headers["set-cookie"]


def test_session_expiry(app):
    c = TestClient(app)
    login(c)
    timeutil.set_now(timeutil.now_utc() + timedelta(hours=13))
    assert c.get("/", follow_redirects=False).status_code == 303
    c2 = TestClient(app)
    login(c2, remember=True)
    assert c2.get("/", follow_redirects=False).status_code == 200


def test_lockout_after_five_failures(app):
    c = TestClient(app)
    for _ in range(5):
        assert login(c, "Edison Young", "nope-nope-nope").status_code == 401
    r = login(c, "Edison Young", TEST_PASSWORD)
    assert r.status_code == 401 and "Too many failed attempts" in r.text
    timeutil.set_now(timeutil.now_utc() + timedelta(minutes=16))
    assert login(c, "Edison Young", TEST_PASSWORD).status_code == 303


def test_csrf_rejected(client, conn):
    lid = add_lead(conn)
    r = client.post("/dial/log", data={"lead_id": lid, "outcome": "no_answer"})
    assert r.status_code == 403
    r = client.post("/dial/log", data={"lead_id": lid, "outcome": "no_answer", "csrf_token": "forged"})
    assert r.status_code == 403
    r = client.post("/dial/log", data={"lead_id": lid, "outcome": "no_answer"}, headers={"X-CSRF-Token": "forged"})
    assert r.status_code == 403
    assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    r = client.post("/dial/log", data={"lead_id": lid, "outcome": "no_answer"}, headers={"X-CSRF-Token": token(client)})
    assert r.status_code == 200


def test_login_requires_csrf(app):
    c = TestClient(app)
    c.get("/login")
    r = c.post("/login", data={"full_name": "Edison Young", "password": TEST_PASSWORD}, follow_redirects=False)
    assert r.status_code == 403


def test_dial_logs_outcome_and_returns_next(client, conn, edison):
    a = add_lead(conn, name="First Lead", score=90)
    b = add_lead(conn, name="Second Lead", score=50)
    page = client.get("/dial")
    assert "First Lead" in page.text and "TPS not checked" in page.text
    r = client.post("/dial/log", data={"lead_id": a, "outcome": "no_answer", "duration": "35"},
                    headers={"X-CSRF-Token": csrf_of(page.text), "HX-Request": "true"})
    assert r.status_code == 200
    assert "Second Lead" in r.text and "First Lead" not in r.text
    assert "No answer logged" in r.headers["HX-Trigger"]
    row = conn.execute("SELECT * FROM calls WHERE lead_id=?", (a,)).fetchone()
    assert row["outcome"] == "no_answer" and row["user_id"] == edison.id and row["duration_seconds"] == 35
    assert leads.get_lead(conn, a)["status"] == "called"
    _ = b


def test_dial_meeting_toast_has_calendar(client, conn):
    a = add_lead(conn, name="Meet Me")
    t = token(client)
    r = client.post("/dial/log", data={"lead_id": a, "outcome": "meeting_booked", "meeting_at": "2026-10-20T14:00"},
                    headers={"X-CSRF-Token": t})
    assert r.status_code == 200
    trig = r.headers["HX-Trigger"]
    assert ".ics" in trig and "calendar.google.com" in trig


def test_dial_objections_saved(client, conn):
    a = add_lead(conn)
    t = token(client)
    r = client.post("/dial/log", data={"lead_id": a, "outcome": "not_interested", "objections": ["too_expensive", "bad_timing"]},
                    headers={"X-CSRF-Token": t})
    assert r.status_code == 200
    tags = sorted(r[0] for r in conn.execute("SELECT tag FROM call_objections").fetchall())
    assert tags == ["bad_timing", "too_expensive"]
    assert "Too expensive" in client.get("/objections").text


def test_dnc_numbers_in_other_formats_blocked_from_dial(client, conn, edison):
    blocked = add_lead(conn, name="Blocked Biz", phone="01293 456789", score=99)
    ok = add_lead(conn, name="Fine Biz", phone="01293 111000", score=10)
    t = token(client)
    r = client.post("/dnc/add", data={"phone": "+44 (0)1293-456 789", "reason": "Asked", "csrf_token": t}, follow_redirects=False)
    assert r.status_code == 303
    card = client.get("/dial").text.split('id="dial-card"')[1]
    assert "Blocked Biz" not in card and "Fine Biz" in card
    direct = client.get(f"/dial?lead={blocked}")
    assert "Do Not Call list" in direct.text
    _ = ok


def test_dial_dnc_button(client, conn):
    a = add_lead(conn, name="Angry", phone="07700 900555", email="angry@ltd.co.uk")
    r = client.post("/dial/dnc", data={"lead_id": a, "reason": "Rude", "do_not_email": "1"}, headers={"X-CSRF-Token": token(client)})
    assert r.status_code == 200
    assert leads.is_dnc(conn, "+447700900555")
    assert conn.execute("SELECT 1 FROM suppression WHERE email='angry@ltd.co.uk'").fetchone()
    assert leads.get_lead(conn, a)["status"] == "dnc"


def test_dnc_remove_needs_confirmation(client, conn, edison):
    leads.dnc_add(conn, "01293 222333", None, edison.id)
    t = token(client)
    assert client.post("/dnc/remove", data={"phone": "01293 222333", "csrf_token": t}).status_code == 400
    assert leads.is_dnc(conn, "01293 222333")
    client.post("/dnc/remove", data={"phone": "01293 222333", "confirm": "yes", "csrf_token": t})
    assert not leads.is_dnc(conn, "01293 222333")


def test_outreach_refuses_sole_trader(client, conn, settings, edison):
    from leadfinder import emailer

    lid = add_lead(conn, name="Solo Sparks", email="solo@sparks.co.uk", entity_type="likely_sole_trader")
    emailer.draft(conn, settings, edison.id)
    eid = conn.execute("SELECT id FROM emails WHERE lead_id=?", (lid,)).fetchone()[0]
    page = client.get("/outreach")
    assert "Sole trader: email not allowed, call instead" in page.text
    t = csrf_of(page.text)
    client.post(f"/outreach/{eid}/approve", headers={"X-CSRF-Token": t})
    r = client.post("/outreach/send", headers={"X-CSRF-Token": t})
    assert r.status_code == 200
    assert "Sole trader" in r.headers["HX-Trigger"]
    assert conn.execute("SELECT status FROM emails WHERE id=?", (eid,)).fetchone()[0] == "approved"
    app_clients = client.app  # mailer never called
    _ = app_clients


def test_attribution_across_actions(client, conn, settings, jack):
    # Log in as Jack in a second client and act; everything is attributed to Jack.
    from fastapi.testclient import TestClient

    c2 = TestClient(client.app)
    login(c2, "Jack Alcock")
    t = token(c2)
    a = add_lead(conn, name="Attrib", email="x@attrib.co.uk")
    c2.post("/dial/log", data={"lead_id": a, "outcome": "not_interested", "objections": ["bad_timing"]}, headers={"X-CSRF-Token": t})
    c2.post(f"/leads/{a}/stage", data={"stage": "lost", "reason": "No"}, headers={"X-CSRF-Token": t})
    c2.post(f"/previews/{a}/generate", data={"csrf_token": t})
    c2.post("/dnc/add", data={"phone": "01293 999111", "csrf_token": t})
    c2.post("/outreach/draft", headers={"X-CSRF-Token": t})
    assert conn.execute("SELECT user_id FROM calls WHERE lead_id=?", (a,)).fetchone()[0] == jack.id
    assert conn.execute("SELECT user_id FROM call_objections").fetchone()[0] == jack.id
    assert conn.execute("SELECT user_id FROM stage_changes WHERE lead_id=?", (a,)).fetchone()[0] == jack.id
    assert conn.execute("SELECT user_id FROM dnc").fetchone()[0] == jack.id
    assert conn.execute("SELECT user_id FROM activity WHERE kind='preview'").fetchone()[0] == jack.id
    kinds = {r[0] for r in conn.execute("SELECT DISTINCT user_id FROM activity WHERE user_id IS NOT NULL")}
    assert kinds == {jack.id}


def test_soft_lock_message_for_partner(client, conn, edison, jack):
    a = add_lead(conn, name="Locked One", score=99)
    client.get("/dial")  # Edison opens it
    c2 = TestClient(client.app)
    login(c2, "Jack Alcock")
    assert "Locked One" not in c2.get("/dial").text.split('id="dial-card"')[1]
    assert "Edison is on this one" in c2.get(f"/dial?lead={a}").text


def test_ics_download(client, conn, settings, edison):
    lid = add_lead(conn, name="Ics, Test; Ltd", phone="01293 123456", address="1 High St, Crawley")
    res = calls.log_call(conn, settings, lid, edison.id, "meeting_booked", meeting_at=timeutil.parse_local("2026-10-20T14:00"),
                         note="Bring examples")
    r = client.get(f"/meetings/{res['meeting_id']}.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    body = r.text
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.endswith("END:VCALENDAR\r\n")
    assert "DTSTART;TZID=Europe/London:20261020T140000" in body
    assert "DTEND;TZID=Europe/London:20261020T143000" in body
    assert "TZID:Europe/London" in body and "BEGIN:VTIMEZONE" in body
    assert "SUMMARY:Meeting: Ics\\, Test\\; Ltd" in body
    assert "Bring examples" in body.replace("\r\n ", "")
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))


def test_settings_never_shows_key_values(client, settings):
    page = client.get("/settings").text
    assert "GOOGLE_API_KEY" in page
    assert settings.google_api_key not in page
    assert settings.secret_key not in page


def test_change_password_requires_current(client, conn, edison):
    from leadfinder import users

    t = token(client)
    client.post("/settings/password", data={"current": "wrong-one!", "new": "new-pass-123", "again": "new-pass-123", "csrf_token": t})
    assert users.authenticate(conn, "Edison Young", TEST_PASSWORD)
    client.post("/settings/password", data={"current": TEST_PASSWORD, "new": "new-pass-123", "again": "new-pass-123", "csrf_token": t})
    assert users.authenticate(conn, "Edison Young", "new-pass-123")


def test_logout_clears_session(client):
    t = token(client)
    client.post("/logout", data={"csrf_token": t})
    assert client.get("/", follow_redirects=False).status_code == 303


def test_pipeline_move(client, conn):
    a = add_lead(conn, status="meeting_booked")
    r = client.post("/pipeline/move", data={"lead_id": a, "stage": "won", "setup": "900", "retainer": "100"},
                    headers={"X-CSRF-Token": token(client)})
    assert r.status_code == 204
    assert leads.get_lead(conn, a)["setup_fee"] == 900


def test_search_from_ui(client):
    import time

    t = token(client)
    r = client.post("/search", data={"trade": "plumbers", "town": "Crawley", "max_leads": "3", "no_audit": "1"},
                    headers={"X-CSRF-Token": t})
    assert r.status_code == 200
    job = re.search(r"/search/job/([0-9a-f]+)", r.text).group(1)
    for _ in range(100):
        p = client.get(f"/search/job/{job}")
        if "Searching" not in p.text:
            break
        time.sleep(0.05)
    assert "qualified" in p.text and "Start dialling" in p.text


def test_campaigns_on_outreach_page(client, conn, settings):
    from leadfinder import campaigns as camp

    for i in range(2):
        add_lead(conn, name=f"US HVAC {i}", trade="hvac", town="Austin, TX", country="US",
                 phone=f"+1 512 555 010{i}", email=f"info{i}@ushvac{i}.com")
    t = token(client)
    h = {"X-CSRF-Token": t}
    r = client.post("/campaigns", data={"name": "hvac-austin", "country": "US", "trade": "hvac",
                                        "website": ["none", "poor"], "limit": "50"}, headers=h)
    assert r.status_code == 200 and "hvac-austin" in r.text and "Drafted 2" in r.headers.get("HX-Trigger", "")
    # Campaign drafts stay out of the one-off review queue.
    assert "US HVAC 0" not in r.text.split("Review queue")[1].split("Approved, waiting")[0]
    cid = camp.get(conn, "hvac-austin")["id"]
    r = client.post(f"/campaigns/{cid}/approve", headers=h)
    assert r.status_code == 200 and camp.stats(conn, cid)["queued"] == 2
    r = client.post(f"/campaigns/{cid}/pause", headers=h)
    assert camp.stats(conn, cid)["queued"] == 0 and camp.stats(conn, cid)["pending"] == 2
    r = client.post("/campaigns", data={"name": "hvac-austin", "country": "US"}, headers=h)
    assert "already exists" in r.headers.get("HX-Trigger", "")
    assert client.post(f"/campaigns/{cid}/nope", headers=h).status_code == 404
    assert client.post("/campaigns", data={"name": "x"}).status_code in (400, 403)  # CSRF still enforced


def test_us_lead_panel_has_no_uk_company_rule(client, conn):
    lid = add_lead(conn, name="Austin Air", country="US", phone="+1 512 555 0199", town="Austin")
    html = client.get(f"/leads/{lid}/panel").text
    assert "Company type unknown" not in html and "Mark limited company" not in html
    uk = add_lead(conn, name="Crawley Co")
    assert "Company type unknown" in client.get(f"/leads/{uk}/panel").text
