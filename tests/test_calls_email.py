from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from leadfinder import calls, emailer, leads, meetings, timeutil
from leadfinder.leads import LeadError
from leadfinder.timeutil import now_utc, to_db

from .fakes import FakeMailer, fake_clients
from .helpers import add_lead


# ---------------------------------------------------------------------------
# next ordering, TPS, locks
# ---------------------------------------------------------------------------

def test_next_ordering(conn, edison, jack):
    low = add_lead(conn, name="Low", score=40)
    high = add_lead(conn, name="High", score=90)
    cb = add_lead(conn, name="Callback today", score=10, status="callback", owner_id=edison.id,
                  callback_at=to_db(now_utc() + timedelta(hours=2)))
    add_lead(conn, name="Callback next week", score=99, status="callback", owner_id=edison.id,
             callback_at=to_db(now_utc() + timedelta(days=7)))
    add_lead(conn, name="Jack's callback", score=99, status="callback", owner_id=jack.id,
             callback_at=to_db(now_utc() + timedelta(hours=1)))
    tired = add_lead(conn, name="Three no answers", score=95, status="called", consecutive_no_answer=3,
                     last_called_at=to_db(now_utc() - timedelta(days=1)))
    retry = add_lead(conn, name="Retry", score=70, status="called", consecutive_no_answer=1,
                     last_called_at=to_db(now_utc() - timedelta(days=1)))
    order = [r["id"] for r in leads.queue(conn, edison.id, limit=10)]
    assert order == [cb, high, low, retry, tired]


def test_next_excludes_tps_registered_and_dnc_and_closed(conn, edison):
    add_lead(conn, name="TPS", score=99, tps_status="registered")
    add_lead(conn, name="Won", score=99, status="won")
    add_lead(conn, name="No phone", score=99, phone="")
    leads.dnc_add(conn, "01293 999 000", "asked", edison.id)
    add_lead(conn, name="DNC", score=99, phone="+44 1293 999000")
    ok = add_lead(conn, name="OK", score=10)
    assert [r["id"] for r in leads.queue(conn, edison.id)] == [ok]


def test_tps_warning_when_unchecked(conn, edison):
    lid = add_lead(conn)
    lead = leads.next_lead(conn, edison.id)
    assert lead["id"] == lid
    assert "TPS not checked" in leads.tps_warning(lead)
    leads.set_tps(conn, lid, False, edison.id)
    assert leads.tps_warning(leads.get_lead(conn, lid)) is None
    leads.set_tps(conn, lid, True, edison.id)
    assert leads.next_lead(conn, edison.id) is None


def test_soft_lock_prevents_double_serving(conn, edison, jack):
    a = add_lead(conn, name="A", score=90)
    b = add_lead(conn, name="B", score=80)
    assert leads.next_lead(conn, edison.id)["id"] == a  # locks A to Edison
    assert leads.next_lead(conn, jack.id)["id"] == b
    assert leads.lock_lead(conn, a, jack.id) == "Edison is on this one"
    # Lock expires after 10 minutes
    timeutil.set_now(now_utc() + timedelta(minutes=11))
    assert leads.lock_lead(conn, a, jack.id) is None


def test_partner_owned_new_leads_wait(conn, edison, jack):
    mine = add_lead(conn, name="Mine", score=10, owner_id=edison.id)
    add_lead(conn, name="Jack's fresh lead", score=90, owner_id=jack.id)
    assert [r["id"] for r in leads.queue(conn, edison.id)] == [mine]


# ---------------------------------------------------------------------------
# Logging calls and stages
# ---------------------------------------------------------------------------

def test_log_call_updates_status_and_owner(conn, settings, edison, jack):
    lid = add_lead(conn, owner_id=jack.id)
    res = calls.log_call(conn, settings, lid, edison.id, "no_answer", duration_seconds=12)
    lead = res["lead"]
    assert lead["status"] == "called" and lead["consecutive_no_answer"] == 1
    assert lead["owner_id"] == edison.id  # first caller becomes owner
    row = conn.execute("SELECT * FROM calls WHERE id=?", (res["call_id"],)).fetchone()
    assert row["user_id"] == edison.id and row["duration_seconds"] == 12 and row["started_at"] and row["ended_at"]
    calls.log_call(conn, settings, lid, jack.id, "callback", callback_at=now_utc() + timedelta(days=2))
    lead = leads.get_lead(conn, lid)
    assert lead["status"] == "callback" and lead["owner_id"] == edison.id and lead["consecutive_no_answer"] == 0


def test_three_no_answers_go_to_bottom(conn, settings, edison):
    a = add_lead(conn, name="A", score=90)
    b = add_lead(conn, name="B", score=20)
    for _ in range(3):
        calls.log_call(conn, settings, a, edison.id, "no_answer")
        timeutil.set_now(now_utc() + timedelta(hours=5))
    order = [r["id"] for r in leads.queue(conn, edison.id)]
    assert order == [b, a]


def test_meeting_needs_time_and_creates_meeting(conn, settings, edison):
    lid = add_lead(conn)
    with pytest.raises(LeadError):
        calls.log_call(conn, settings, lid, edison.id, "meeting_booked")
    when = timeutil.parse_local("2026-10-20T14:00")
    res = calls.log_call(conn, settings, lid, edison.id, "meeting_booked", meeting_at=when, note="Wants a quote")
    m = meetings.get_meeting(conn, res["meeting_id"])
    assert m["start_at"] == "2026-10-20 13:00:00"  # 14:00 BST = 13:00 UTC
    assert res["lead"]["status"] == "meeting_booked"


def test_stage_won_records_fees(conn, settings, edison):
    lid = add_lead(conn, status="meeting_booked")
    calls.set_stage(conn, lid, edison.id, "proposal_sent")
    lead = calls.set_stage(conn, lid, edison.id, "won", setup_fee=800, retainer=150)
    assert lead["status"] == "won" and lead["setup_fee"] == 800 and lead["retainer"] == 150
    rows = conn.execute("SELECT to_stage, user_id FROM stage_changes WHERE lead_id=? ORDER BY id", (lid,)).fetchall()
    assert [r["to_stage"] for r in rows] == ["proposal_sent", "won"] and all(r["user_id"] == edison.id for r in rows)
    lost = calls.set_stage(conn, add_lead(conn), edison.id, "lost", reason="Went with cousin")
    assert lost["lost_reason"] == "Went with cousin"


def test_objection_tags_saved_and_counted(conn, settings, edison):
    a, b = add_lead(conn), add_lead(conn)
    calls.log_call(conn, settings, a, edison.id, "not_interested", objections=["too_expensive", "use_socials"])
    calls.log_call(conn, settings, b, edison.id, "callback", objections=["too_expensive", "other"], objection_other="Wife decides")
    counts = {c["tag"]: c["n"] for c in calls.objection_counts(conn)}
    assert counts == {"too_expensive": 2, "use_socials": 1, "other": 1}
    other = conn.execute("SELECT other_text, user_id FROM call_objections WHERE tag='other'").fetchone()
    assert other["other_text"] == "Wife decides" and other["user_id"] == edison.id


def test_parse_duration():
    assert calls.parse_duration("95") == 95
    assert calls.parse_duration("1:35") == 95
    assert calls.parse_duration("2m5s") == 125
    assert calls.parse_duration(None) == 0


# ---------------------------------------------------------------------------
# DNC
# ---------------------------------------------------------------------------

def test_dnc_formats_match(conn, edison):
    lid = add_lead(conn, phone="01293 456789")
    leads.dnc_add(conn, "+44 (0)1293 456 789", "Asked", edison.id)
    assert leads.is_dnc(conn, "01293456789")
    assert leads.is_dnc(conn, "0044 1293 456789")
    assert leads.get_lead(conn, lid)["status"] == "dnc"
    assert leads.next_lead(conn, edison.id) is None


def test_export_csv_excel_friendly_and_skips_dnc(conn, edison, tmp_path):
    add_lead(conn, name="Café Ünïcode")
    add_lead(conn, name="Blocked", phone="01293 111222")
    leads.dnc_add(conn, "01293 111222", None, edison.id)
    out = tmp_path / "out.csv"
    n = leads.export_csv(conn, out)
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # utf-8-sig BOM for Excel
    text = raw.decode("utf-8-sig")
    assert n == 1 and "Café Ünïcode" in text and "Blocked" not in text
    assert "\r\n" in text  # csv module line endings, newline='' honoured


# ---------------------------------------------------------------------------
# Email rules
# ---------------------------------------------------------------------------

def _approved(conn, settings, user, **lead_kw):
    lid = add_lead(conn, email=lead_kw.pop("email", f"info@biz{datetime.now().microsecond}{id(lead_kw)}.co.uk"), **lead_kw)
    rep = emailer.draft(conn, settings, user.id, limit=50)
    eid = conn.execute("SELECT id FROM emails WHERE lead_id=?", (lid,)).fetchone()[0]
    assert eid in rep.created
    emailer.approve(conn, eid, user.id)
    return lid, eid


def test_template_rendering_and_footer(conn, settings, edison):
    lid = add_lead(conn, name="Smith Plumbing", email="info@smith.co.uk", hook="142 reviews at 4.8 stars but no website")
    emailer.draft(conn, settings, edison.id)
    e = conn.execute("SELECT * FROM emails WHERE lead_id=?", (lid,)).fetchone()
    assert e["status"] == "pending_review"
    assert e["subject"] == "Quick idea for Smith Plumbing"
    body = e["body"]
    assert "Smith Plumbing" in body and "plumbers in Crawley" in body and "142 reviews at 4.8 stars but no website" in body
    assert "{preview_url}" not in body and "mocked up" not in body  # no preview -> line dropped
    assert "Edison Young" in body and "1 Test Street, Crawley RH10 1AA" in body
    assert emailer.STOP_LINE in body
    assert len(body.split("\n--\n")[0].split()) < 120


def test_template_includes_preview_when_present(conn, settings, edison):
    lid = add_lead(conn, email="info@x.co.uk", preview_url="https://demo.netlify.app")
    emailer.draft(conn, settings, edison.id)
    assert "https://demo.netlify.app" in conn.execute("SELECT body FROM emails WHERE lead_id=?", (lid,)).fetchone()[0]


def test_too_long_template_refused(conn, settings, edison):
    (settings.templates_dir / "cold_email.txt").write_text("Subject: Hi\n\n" + "word " * 130, encoding="utf-8")
    add_lead(conn, email="a@b.co.uk")
    rep = emailer.draft(conn, settings, edison.id)
    assert rep.created == [] and "words" in rep.skipped[0][1]


def test_sole_trader_blocked(conn, settings, edison):
    _approved(conn, settings, edison, entity_type="likely_sole_trader")
    _approved(conn, settings, edison, entity_type="unknown")
    mailer = FakeMailer()
    rep = emailer.send(conn, settings, fake_clients(mailer=mailer), edison.id, sleep=lambda s: None)
    assert mailer.sent == []
    reasons = rep.skip_summary()
    assert any("Sole trader" in r for r in reasons) and any("unknown" in r for r in reasons)


def test_send_to_limited_company_with_headers(conn, settings, edison):
    lid, eid = _approved(conn, settings, edison, entity_type="limited_company", email="info@ltd.co.uk")
    mailer = FakeMailer()
    rep = emailer.send(conn, settings, fake_clients(mailer=mailer), edison.id, sleep=lambda s: None)
    assert len(rep.sent) == 1
    msg = mailer.sent[0]
    assert msg["To"] == "info@ltd.co.uk" and msg["Bcc"] is None and msg["Cc"] is None
    assert msg["List-Unsubscribe"] == "<mailto:hello@example.org?subject=unsubscribe>"
    assert msg.get_content_type() == "text/plain"
    row = conn.execute("SELECT status, sent_at, sent_by FROM emails WHERE id=?", (eid,)).fetchone()
    assert row["status"] == "sent" and row["sent_at"] and row["sent_by"] == edison.id


def test_daily_limit_counted_from_db(conn, settings, edison):
    # DAILY_SEND_LIMIT=3 in the test .env; one already sent today.
    lid0 = add_lead(conn, email="old@x.co.uk", entity_type="limited_company")
    conn.execute("INSERT INTO emails(lead_id, to_email, subject, body, status, created_at, sent_at) VALUES (?,?,?,?,?,?,?)",
                 (lid0, "old@x.co.uk", "s", "b", "sent", timeutil.now_db(), timeutil.now_db()))
    conn.commit()
    for i in range(4):
        _approved(conn, settings, edison, entity_type="limited_company", email=f"info{i}@ltd{i}.co.uk")
    mailer = FakeMailer()
    waits = []
    rep = emailer.send(conn, settings, fake_clients(mailer=mailer), edison.id, sleep=waits.append, rand=lambda a, b: 90.0)
    assert len(mailer.sent) == 2 and rep.stopped_at_limit
    assert waits == [90.0]  # random 60-180s wait between sends (not before the first)


def test_random_wait_range(conn, settings, edison):
    for i in range(2):
        _approved(conn, settings, edison, entity_type="limited_company", email=f"w{i}@ltd.co.uk")
    waits = []
    emailer.send(conn, settings, fake_clients(), edison.id, sleep=waits.append)
    assert len(waits) == 1 and 60 <= waits[0] <= 180


def test_suppression_blocks(conn, settings, edison):
    lid, eid = _approved(conn, settings, edison, entity_type="limited_company", email="stop@ltd.co.uk")
    emailer.suppress(conn, "STOP@ltd.co.uk", edison.id)
    mailer = FakeMailer()
    emailer.send(conn, settings, fake_clients(mailer=mailer), edison.id, sleep=lambda s: None)
    assert mailer.sent == []
    # and no new draft is created for a suppressed address
    add_lead(conn, email="stop@ltd.co.uk")
    rep = emailer.draft(conn, settings, edison.id)
    assert rep.created == [] and "suppression" in rep.skipped[0][1]


def test_no_double_send_without_followup(conn, settings, edison):
    lid, eid = _approved(conn, settings, edison, entity_type="limited_company", email="once@ltd.co.uk")
    emailer.send(conn, settings, fake_clients(), edison.id, sleep=lambda s: None)
    # draft() never drafts a second first email
    assert emailer.draft(conn, settings, edison.id).created == []
    # a sneaky second approved initial email is refused at send time
    conn.execute("INSERT INTO emails(lead_id, kind, to_email, subject, body, status, created_at) VALUES (?,?,?,?,?,?,?)",
                 (lid, "initial", "once@ltd.co.uk", "s", "hi\n\n--\n" + emailer.STOP_LINE, "approved", timeutil.now_db()))
    conn.commit()
    mailer = FakeMailer()
    rep = emailer.send(conn, settings, fake_clients(mailer=mailer), edison.id, sleep=lambda s: None)
    assert mailer.sent == [] and "Already emailed this lead" in rep.skip_summary()
    # follow-up only after 5 days
    assert emailer.followup(conn, settings, edison.id).created == []
    timeutil.set_now(now_utc() + timedelta(days=5, minutes=1))
    rep = emailer.followup(conn, settings, edison.id)
    assert len(rep.created) == 1
    assert emailer.followup(conn, settings, edison.id).created == []  # only one follow-up
    emailer.approve(conn, rep.created[0], edison.id)
    mailer = FakeMailer()
    emailer.send(conn, settings, fake_clients(mailer=mailer), edison.id, sleep=lambda s: None)
    assert len(mailer.sent) == 1 and mailer.sent[0]["Subject"].startswith("Re:")


def test_dry_run_sends_nothing(conn, settings, edison):
    _approved(conn, settings, edison, entity_type="limited_company", email="dry@ltd.co.uk")
    mailer = FakeMailer()
    rep = emailer.send(conn, settings, fake_clients(mailer=mailer), edison.id, dry_run=True)
    assert mailer.sent == [] and len(rep.would_send) == 1
    assert "To: dry@ltd.co.uk" in rep.would_send[0]["message"]
    assert conn.execute("SELECT status FROM emails").fetchone()[0] == "approved"


def test_edit_keeps_footer(conn, settings, edison):
    _, eid = _approved(conn, settings, edison, entity_type="limited_company")
    with pytest.raises(emailer.EmailError):
        emailer.edit(conn, eid, "Hi", "No footer here", edison.id)
    e = emailer.get_email(conn, eid)
    emailer.edit(conn, eid, "New subject", e["body"].replace("Hi there", "Hello"), edison.id)
    assert emailer.get_email(conn, eid)["status"] == "pending_review"


def test_unsubscribe_extract():
    text = "From: Bob <bob@builder.co.uk>\nSTOP\n> On Tue, info@acme.co.uk wrote"
    assert emailer.extract_addresses(text) == ["bob@builder.co.uk", "info@acme.co.uk"]


def test_add_email_validates(conn, edison):
    lid = add_lead(conn)
    with pytest.raises(emailer.EmailError):
        emailer.add_email(conn, lid, "not-an-email", edison.id)
    emailer.add_email(conn, lid, "Owner@Biz.co.uk", edison.id)
    assert leads.get_lead(conn, lid)["email"] == "owner@biz.co.uk"
