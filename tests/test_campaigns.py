"""Batch campaigns, US/UK country rules, warm-up, bounces, and US search."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from leadfinder import campaigns as camp, db, emailer, phones
from leadfinder.config import load_settings
from leadfinder.search import SearchOptions, run_search
from leadfinder.timeutil import now_db, now_utc, to_db

from .fakes import FakeMailer, FakePlaces, fake_clients, place
from .helpers import add_lead

AUSTIN = {"latitude": 30.2672, "longitude": -97.7431}


def us_lead(conn, **kw):
    kw.setdefault("phone", "+1 512 555 0142")
    kw.setdefault("town", "Austin, TX")
    kw.setdefault("trade", "hvac")
    kw.setdefault("address", "100 Congress Ave, Austin, TX 78701, USA")
    kw.setdefault("postcode", "78701")
    return add_lead(conn, country="US", **kw)


def no_wait(**kw):
    return dict(sleep=lambda s: None, rand=lambda a, b: 0.0, **kw)


# ---------------------------------------------------------------------------
# Phones and search
# ---------------------------------------------------------------------------

def test_us_phone_numbers():
    assert phones.us_storage("(512) 555-0142") == "+1 512 555 0142"
    info = phones.classify("+1 512 555 0142")
    assert info.valid and info.kind == phones.US and info.e164 == "+15125550142"
    assert phones.pretty("+1 512 555 0142") == "(512) 555-0142"
    assert not phones.classify("(123) 456-7890", "US").valid  # area code can't start with 1
    assert phones.classify("01293 555123").kind == phones.LANDLINE  # UK unchanged


def test_us_search_uses_us_region_and_keeps_us_numbers(conn, settings, edison):
    hvac = [
        place("u1", "Lone Star Heating & Air", "(512) 555-0101", None, 4.8, 120, ptype="hvac_contractor",
              lat=30.27, lng=-97.74, address="1 Main St, Austin, TX 78701, USA"),
        place("u2", "Cool Breeze HVAC", "(512) 555-0102", "https://coolbreeze.wixsite.com/home", 4.6, 40,
              ptype=None, lat=30.28, lng=-97.75, address="2 Oak St, Austin, TX 78702, USA"),
    ]
    places = FakePlaces(pages={"hvac in Austin, TX, USA": [hvac]}, town_results={"Austin, TX, USA": AUSTIN})
    rep = run_search(conn, settings, fake_clients(places=places), edison,
                     SearchOptions(trade="hvac", town="Austin, TX", country="US", max_leads=5, audit=False),
                     sleep=lambda s: None)
    geo, first = places.calls[0]["body"], places.calls[1]["body"]
    assert geo["regionCode"] == "US" and geo["textQuery"].endswith(", USA")
    assert first["regionCode"] == "US" and first["textQuery"].endswith(", USA")
    assert {l["name"] for l in rep.new_leads} == {"Lone Star Heating & Air", "Cool Breeze HVAC"}
    row = conn.execute("SELECT * FROM leads WHERE place_id='u1'").fetchone()
    assert row["country"] == "US" and row["phone_e164"] == "+15125550101" and row["postcode"] == "78701"
    assert row["entity_type"] == "unknown"  # no Companies House / sole-trader guess for US
    assert conn.execute("SELECT country FROM searches").fetchone()[0] == "US"


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA)  # the first release's tables, without the columns added since
    assert "country" not in {r[1] for r in old.execute("PRAGMA table_info(leads)")}
    old.commit()
    old.close()
    conn = db.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(leads)")}
    assert "country" in cols
    assert {"campaign_id", "bounced_at"} <= {r[1] for r in conn.execute("PRAGMA table_info(emails)")}
    conn.close()


# ---------------------------------------------------------------------------
# Country send rules
# ---------------------------------------------------------------------------

def test_us_leads_need_no_company_type_but_need_postal_address(conn, settings, edison, home):
    lid = us_lead(conn, email="info@lonestar.com", entity_type="unknown")
    uk = add_lead(conn, email="info@crawleyplumb.co.uk", entity_type="unknown")
    camp.create(conn, settings, edison.id, "mixed-us", camp.make_filters("US"))
    e = conn.execute("SELECT * FROM emails WHERE lead_id=?", (lid,)).fetchone()
    assert emailer.block_reason_for(conn, dict(e), settings) is None
    # The UK lead isn't in a US campaign at all, and on its own a UK unknown type is still blocked.
    assert conn.execute("SELECT COUNT(*) FROM emails WHERE lead_id=?", (uk,)).fetchone()[0] == 0
    (home / ".env").write_text((home / ".env").read_text().replace("BUSINESS_ADDRESS=1 Test Street, Crawley RH10 1AA\n", ""))
    no_addr = load_settings(home)
    assert "BUSINESS_ADDRESS" in emailer.block_reason_for(conn, dict(e), no_addr)


def test_uk_campaign_keeps_pecr_rules(conn, settings, edison):
    ltd = add_lead(conn, name="Ltd", email="a@ltd.co.uk", entity_type="limited_company", score=80)
    sole = add_lead(conn, name="Sole", email="b@sole.co.uk", entity_type="likely_sole_trader", score=70)
    res = camp.create(conn, settings, edison.id, "uk-plumbers", camp.make_filters("GB", trade="plumbers"))
    assert res.drafted == 2 and res.blocked == {"Sole trader: email not allowed, call instead": 1}
    ap = camp.approve(conn, settings, edison.id, "uk-plumbers")
    assert ap.approved == 1
    status = dict(conn.execute("SELECT lead_id, status FROM emails").fetchall())
    assert status == {ltd: "approved", sole: "pending_review"}


# ---------------------------------------------------------------------------
# Campaign lifecycle
# ---------------------------------------------------------------------------

def test_segment_filters_and_create(conn, settings, edison):
    a = us_lead(conn, name="No site", email="a@a.com", website_status="none", score=90)
    b = us_lead(conn, name="Poor site", email="b@b.com", website_status="poor", score=60)
    us_lead(conn, name="Good site", email="c@c.com", website_status="good", score=95)
    us_lead(conn, name="Other trade", email="d@d.com", trade="roofers")
    us_lead(conn, name="No email", email=None)
    us_lead(conn, name="Lost", email="e@e.com", status="lost")
    add_lead(conn, name="UK one", email="f@f.co.uk", trade="hvac")
    f = camp.make_filters("US", trade="hvac")
    assert camp.segment_counts(conn, f) == {"matched": 3, "with_email": 2, "available": 2}
    res = camp.create(conn, settings, edison.id, "hvac-austin", f)
    assert res.drafted == 2 and [s["lead_id"] for s in res.samples] == [a, b]  # best score first
    body = res.samples[0]["body"]
    assert "No site" in res.samples[0]["subject"] and emailer.STOP_LINE in body and "1 Test Street" in body
    with pytest.raises(camp.CampaignError, match="already exists"):
        camp.create(conn, settings, edison.id, "HVAC-Austin", f)
    # Topping up finds only new leads.
    assert camp.extend(conn, settings, edison.id, "hvac-austin").drafted == 0
    us_lead(conn, name="New one", email="g@g.com")
    assert camp.extend(conn, settings, edison.id, "hvac-austin").drafted == 1


def test_bad_inputs(conn, settings, edison):
    with pytest.raises(camp.CampaignError, match="Unknown website status"):
        camp.make_filters("US", website="none,terrible")
    with pytest.raises(camp.CampaignError, match="isn't supported"):
        camp.make_filters("FR")
    with pytest.raises(camp.CampaignError, match="not found"):
        camp.create(conn, settings, edison.id, "x", camp.make_filters("US"), template="nope.txt")
    with pytest.raises(camp.CampaignError, match="No campaign"):
        camp.get(conn, "missing")


def test_send_respects_daily_limit_and_runs_over_days(conn, settings, edison):
    for i in range(5):
        us_lead(conn, name=f"Biz {i}", email=f"x{i}@biz{i}.com", score=90 - i)
    camp.create(conn, settings, edison.id, "big", camp.make_filters("US"))
    camp.approve(conn, settings, edison.id, "big")
    mailer = FakeMailer()
    rep = camp.send(conn, settings, fake_clients(mailer=mailer), edison.id, "big", **no_wait())
    assert len(rep.sent) == 3 and rep.stopped_at_limit  # DAILY_SEND_LIMIT=3 in tests
    msg = mailer.sent[0]
    assert msg["List-Unsubscribe"] and msg["To"] == "x0@biz0.com" and msg["Bcc"] is None
    st = camp.stats(conn, camp.get(conn, "big")["id"])
    assert st["sent"] == 3 and st["queued"] == 2


def test_only_this_campaign_is_sent(conn, settings, edison):
    us_lead(conn, email="one@a.com")
    camp.create(conn, settings, edison.id, "one", camp.make_filters("US"))
    camp.approve(conn, settings, edison.id, "one")
    other = add_lead(conn, email="two@ltd.co.uk", entity_type="limited_company")
    camp.create(conn, settings, edison.id, "two", camp.make_filters("GB"))
    camp.approve(conn, settings, edison.id, "two")
    mailer = FakeMailer()
    camp.send(conn, settings, fake_clients(mailer=mailer), edison.id, "two", **no_wait())
    assert [m["To"] for m in mailer.sent] == ["two@ltd.co.uk"]
    assert conn.execute("SELECT status FROM emails WHERE lead_id=?", (other,)).fetchone()[0] == "sent"


def test_pause_and_resume(conn, settings, edison):
    us_lead(conn, email="p@p.com")
    camp.create(conn, settings, edison.id, "p", camp.make_filters("US"))
    camp.approve(conn, settings, edison.id, "p")
    assert camp.pause(conn, edison.id, "p") == 1
    with pytest.raises(camp.CampaignError, match="paused"):
        camp.send(conn, settings, fake_clients(), edison.id, "p", **no_wait())
    assert camp.approve(conn, settings, edison.id, "p").approved == 1
    assert len(camp.send(conn, settings, fake_clients(), edison.id, "p", **no_wait()).sent) == 1


def test_followups_join_the_campaign(conn, settings, edison, frozen_time):
    from leadfinder import timeutil

    lid = us_lead(conn, email="f@f.com")
    camp.create(conn, settings, edison.id, "fu", camp.make_filters("US"))
    camp.approve(conn, settings, edison.id, "fu")
    camp.send(conn, settings, fake_clients(), edison.id, "fu", **no_wait())
    assert camp.draft_followups(conn, settings, edison.id, "fu").created == []  # too soon
    timeutil.set_now(now_utc() + timedelta(days=6))
    rep = camp.draft_followups(conn, settings, edison.id, "fu")
    assert len(rep.created) == 1
    e = emailer.get_email(conn, rep.created[0])
    assert e["campaign_id"] == camp.get(conn, "fu")["id"] and e["subject"].startswith("Re:")
    assert camp.approve(conn, settings, edison.id, "fu").approved == 1
    camp.send(conn, settings, fake_clients(), edison.id, "fu", **no_wait())
    assert camp.stats(conn, camp.get(conn, "fu")["id"])["followups"] == 1
    assert lid


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def test_stats_bounces_replies_meetings(conn, settings, edison):
    ids = [us_lead(conn, name=f"S{i}", email=f"s{i}@s{i}.com", score=90 - i) for i in range(3)]
    camp.create(conn, settings, edison.id, "s", camp.make_filters("US"))
    camp.approve(conn, settings, edison.id, "s")
    camp.send(conn, settings, fake_clients(), edison.id, "s", **no_wait())
    assert emailer.mark_bounced(conn, ids[0], edison.id) == "s0@s0.com"
    assert emailer.is_suppressed(conn, "s0@s0.com")
    emailer.mark_replied(conn, ids[1], edison.id)
    conn.execute("INSERT INTO meetings(lead_id, user_id, start_at, created_at) VALUES (?,?,?,?)",
                 (ids[1], edison.id, to_db(now_utc() + timedelta(days=2)), now_db()))
    conn.commit()
    st = camp.stats(conn, camp.get(conn, "s")["id"])
    assert (st["sent"], st["bounced"], st["delivered"], st["replied"], st["meetings"]) == (3, 1, 2, 1, 1)
    assert st["reply_rate"] == 50.0 and st["meeting_rate"] == 50.0 and st["meeting_band"] == "great"
    assert st["emails_per_meeting"] == 2 and st["unsubscribed"] == 0
    with pytest.raises(emailer.EmailError):
        emailer.mark_bounced(conn, us_lead(conn), edison.id)


def test_bands_and_warnings():
    assert camp.band(0.2, camp.MEETING_BANDS) == "below average"
    assert camp.band(0.5, camp.MEETING_BANDS) == "average"
    assert camp.band(1.2, camp.MEETING_BANDS) == "good"
    assert camp.band(4.0, camp.REPLY_BANDS) == "good"
    w = camp.health_warnings({"sent": 100, "bounce_rate": 5.0, "delivered": 95, "replied": 0, "unsubscribed": 0})
    assert any("Bounce rate" in x for x in w)


# ---------------------------------------------------------------------------
# Warm-up
# ---------------------------------------------------------------------------

def test_warmup_ramps_daily_limit(conn, home):
    env = (home / ".env").read_text().replace("DAILY_SEND_LIMIT=3", "DAILY_SEND_LIMIT=40")
    (home / ".env").write_text(env + "WARMUP_START=2026-10-14\n")
    s = load_settings(home)
    assert emailer.warmup_cap(s, date(2026, 10, 16)) == 10
    assert emailer.warmup_cap(s, date(2026, 10, 28)) == 30
    assert emailer.daily_limit(conn, s) == 10  # tests run on 16 Oct
    (home / ".env").write_text(env)
    assert emailer.daily_limit(conn, load_settings(home)) == 40
