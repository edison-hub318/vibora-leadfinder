from __future__ import annotations

from datetime import datetime

import pytest

from leadfinder import phones, search, timeutil
from leadfinder.clients import TEXT_SEARCH_FIELD_MASK
from leadfinder.freetier import PLACE_DETAILS, TEXT_SEARCH, AllowanceReached, UsageGuard
from leadfinder.search import SearchOptions, run_search

from .fakes import FakePlaces, crawley_plumbers, fake_clients, place
from .helpers import add_lead

Q = "plumbers in Crawley, UK"


def run(conn, settings, user, places, **kw):
    clients = fake_clients(places=places)
    opts = SearchOptions(trade=kw.pop("trade", "plumbers"), town=kw.pop("town", "Crawley"),
                         max_leads=kw.pop("max_leads", 30), audit=kw.pop("audit", False), **kw)
    return run_search(conn, settings, clients, user, opts, sleep=lambda s: None)


def names(rep):
    return {lead["name"] for lead in rep.new_leads}


def test_request_shape_and_field_mask(conn, settings, edison):
    places = FakePlaces(pages={Q: [crawley_plumbers()]})
    run(conn, settings, edison, places, max_leads=3)
    body = places.calls[0]["body"]
    assert body["textQuery"] == Q
    assert body["regionCode"] == "GB"
    assert "circle" in body["locationBias"]
    assert body["locationBias"]["circle"]["radius"] == 8000
    mask = places.calls[0]["field_mask"]
    assert mask == TEXT_SEARCH_FIELD_MASK
    for f in ["places.id", "places.displayName", "places.formattedAddress", "places.nationalPhoneNumber",
              "places.websiteUri", "places.rating", "places.userRatingCount", "places.businessStatus",
              "places.primaryType", "places.googleMapsUri", "nextPageToken"]:
        assert f in mask.split(",")


def test_pipeline_filters(conn, settings, edison):
    rep = run(conn, settings, edison, FakePlaces(pages={Q: [crawley_plumbers()]}))
    got = names(rep)
    # Out of area (London) dropped
    assert "Victoria Plumbing London" not in got
    # Closed businesses skipped
    assert "Old Town Plumbers (closed)" not in got
    assert rep.closed_skipped == 1
    # Category: B&Q is a home improvement store
    assert "B&Q Crawley" not in got
    # Chains: British Gas is in chains.txt
    assert "British Gas Crawley" not in got
    # Phone-number duplicate merged, keeping the listing with more reviews
    assert "Smith Plumbing & Heating" in got
    assert "Smith Plumbing and Heating Ltd" not in got
    # No phone: dropped by default
    assert "Ghost Plumbing" not in got
    assert {"Crawley Boiler Repairs", "J Patel Plumbing Ltd", "AquaFlow Heating", "Pound Hill Plumber"} <= got
    labels = [label for label, _ in rep.funnel]
    assert labels[0] == "Raw results" and labels[-1] == "Callable phone"
    assert rep.funnel[-1][1] == rep.qualified


def test_duplicate_listing_is_stored_and_linked(conn, settings, edison):
    run(conn, settings, edison, FakePlaces(pages={Q: [crawley_plumbers()]}))
    primary = conn.execute("SELECT id, review_count FROM leads WHERE place_id='p1'").fetchone()
    dup = conn.execute("SELECT duplicate_of FROM leads WHERE place_id='p9'").fetchone()
    assert dup["duplicate_of"] == primary["id"]
    assert primary["review_count"] == 142


def test_include_no_phone_keeps_ghost_and_flags_it(conn, settings, edison):
    rep = run(conn, settings, edison, FakePlaces(pages={Q: [crawley_plumbers()]}), include_no_phone=True)
    ghost = next(lead for lead in rep.new_leads if lead["name"] == "Ghost Plumbing")
    assert "possible_ghost" in ghost["quality_flags"]


def test_mobile_means_likely_sole_trader(conn, settings, edison):
    rep = run(conn, settings, edison, FakePlaces(pages={Q: [crawley_plumbers()]}))
    smith = next(lead for lead in rep.new_leads if lead["name"] == "Smith Plumbing & Heating")
    assert smith["phone_type"] == "mobile"
    assert smith["entity_type"] == "likely_sole_trader"
    patel = next(lead for lead in rep.new_leads if lead["name"] == "J Patel Plumbing Ltd")
    assert patel["entity_type"] == "unknown"


def test_low_rating_flagged(conn, settings, edison):
    rep = run(conn, settings, edison, FakePlaces(pages={Q: [crawley_plumbers()]}))
    tb = next(lead for lead in rep.new_leads if lead["name"] == "Three Bridges Heating")
    assert "low_rating" in tb["quality_flags"]


def test_pagination_follows_next_page_token(conn, settings, edison):
    page1 = [place(f"a{i}", f"Plumber {i}", f"01293 4000{i:02d}") for i in range(20)]
    page2 = [place(f"b{i}", f"Heating {i}", f"01293 5000{i:02d}") for i in range(20)]
    page3 = [place(f"c{i}", f"Boiler {i}", f"01293 6000{i:02d}") for i in range(5)]
    places = FakePlaces(pages={Q: [page1, page2, page3]})
    sleeps = []
    clients = fake_clients(places=places)
    rep = run_search(conn, settings, clients, edison, SearchOptions("plumbers", "Crawley", max_leads=100, audit=False),
                     sleep=sleeps.append)
    tokens = [c["body"].get("pageToken") for c in places.calls if c["body"]["textQuery"] == Q]
    assert tokens == [None, "tok:1", "tok:2"]
    assert sleeps.count(2.0) >= 2  # waited between pages
    assert rep.qualified == 45


def test_stops_when_enough(conn, settings, edison):
    page1 = [place(f"a{i}", f"Plumber {i}", f"01293 4000{i:02d}") for i in range(20)]
    page2 = [place(f"b{i}", f"Heating {i}", f"01293 5000{i:02d}") for i in range(20)]
    places = FakePlaces(pages={Q: [page1, page2]})
    rep = run(conn, settings, edison, places, max_leads=10)
    assert len(places.calls) == 1
    assert rep.stop_reason == "enough"


def test_synonym_widening_until_n(conn, settings, edison):
    small = [place(f"a{i}", f"Plumber {i}", f"01293 4000{i:02d}") for i in range(4)]
    syn1 = [place(f"s{i}", f"Plumbing Services {i}", f"01293 7000{i:02d}") for i in range(4)]
    syn2 = [place(f"t{i}", f"Emergency Plumber {i}", f"01293 8000{i:02d}") for i in range(4)]
    places = FakePlaces(pages={
        Q: [small],
        "plumbing services in Crawley, UK": [syn1],
        "emergency plumber in Crawley, UK": [syn2],
    })
    rep = run(conn, settings, edison, places, max_leads=8)
    queried = [c["body"]["textQuery"] for c in places.calls]
    assert queried == [Q, "plumbing services in Crawley, UK"]
    assert rep.qualified == 8
    assert rep.stop_reason == "enough"


def test_widening_stops_at_budget(conn, settings, edison):
    places = FakePlaces(default=[[place("x1", "Plumber X", "01293 400001")]])
    rep = run(conn, settings, edison, places, max_leads=50, request_cap=3)
    assert len(places.calls) == 3
    assert rep.stop_reason == "budget"
    assert "budget" in rep.stop_message.lower()


def test_widening_reaches_nearby_towns(conn, settings, edison):
    places = FakePlaces(default=[[]])
    rep = run(conn, settings, edison, places, max_leads=5, request_cap=40)
    queried = [c["body"]["textQuery"] for c in places.calls]
    assert any("in Horley, UK" in q or "in Three Bridges, UK" in q for q in queried)
    assert rep.stop_reason == "exhausted"


def test_already_handled_never_returned_as_new(conn, settings, edison, jack):
    from leadfinder.timeutil import now_db

    add_lead(conn, place_id="p3", name="J Patel Plumbing Ltd", phone="01293 600700", status="won")
    add_lead(conn, place_id="p12", name="Pound Hill Plumber", phone="01293 888999", status="not_interested",
             stage_changed_at=now_db())
    add_lead(conn, place_id="p4", name="AquaFlow Heating", phone="07811 223344", tps_status="registered")
    jid = add_lead(conn, place_id="p2", name="Crawley Boiler Repairs", phone="01293 512345", owner_id=jack.id)
    conn.execute("INSERT INTO calls(lead_id, user_id, outcome, started_at, ended_at, created_at) VALUES (?,?,?,?,?,?)",
                 (jid, jack.id, "no_answer", now_db(), now_db(), now_db()))
    conn.execute("INSERT INTO dnc(phone_e164, created_at) VALUES (?, ?)", (phones.normalise_e164("01293 777888"), now_db()))
    conn.commit()
    rep = run(conn, settings, edison, FakePlaces(pages={Q: [crawley_plumbers()]}))
    got = names(rep)
    for n in ["J Patel Plumbing Ltd", "Pound Hill Plumber", "AquaFlow Heating", "Crawley Boiler Repairs", "Three Bridges Heating"]:
        assert n not in got
    assert rep.handled["client"] == 1
    assert rep.handled["not_interested"] == 1
    assert rep.handled["tps"] == 1
    assert rep.handled["partner"] == 1
    assert rep.handled["dnc"] == 1
    # The new DNC business is stored as blocked immediately
    blocked = conn.execute("SELECT status, blocked_reason FROM leads WHERE place_id='p11'").fetchone()
    assert blocked["status"] == "dnc" and blocked["blocked_reason"]


def test_duplicate_upserts_never_duplicate(conn, settings, edison):
    places = FakePlaces(pages={Q: [crawley_plumbers()]})
    first = run(conn, settings, edison, places)
    n1 = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    second = run(conn, settings, edison, FakePlaces(pages={Q: [crawley_plumbers()]}), force=True)
    n2 = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    assert first.qualified > 0
    assert second.qualified == 0
    assert second.known_count == first.qualified
    assert n1 == n2
    assert second.handled.get("known") == first.qualified


def test_chain_by_shared_domain_and_many_towns(conn, settings, edison):
    add_lead(conn, name="FlowRite Plumbing", town="Horsham", website="https://flowrite.co.uk/horsham")
    add_lead(conn, name="FlowRite Plumbing", town="Reigate", website="https://flowrite.co.uk/reigate")
    page = [
        place("f1", "FlowRite Plumbing", "01293 100200", "https://www.flowrite.co.uk/crawley"),
        place("d1", "Drip Doctor Crawley", "01293 100300", "https://dripdoc.co.uk/a"),
        place("d2", "Drip Doctor Three Bridges", "01293 100400", "https://dripdoc.co.uk/b"),
        place("d3", "Drip Doctor Pound Hill", "01293 100500", "https://dripdoc.co.uk/c"),
        place("ok", "Honest Plumbing", "01293 100600", "https://honestplumbing.co.uk"),
    ]
    rep = run(conn, settings, edison, FakePlaces(pages={Q: [page]}))
    assert names(rep) == {"Honest Plumbing"}


def test_unknown_trade_skips_category_filter(conn, settings, edison):
    page = [place("u1", "Crawley Saddlery", "01293 222333", ptype="store")]
    rep = run(conn, settings, edison, FakePlaces(pages={"saddlers in Crawley, UK": [page]}), trade="saddlers")
    assert names(rep) == {"Crawley Saddlery"}


def test_repeat_search_warning_requires_force(conn, settings, edison, jack):
    run(conn, settings, jack, FakePlaces(pages={Q: [crawley_plumbers()]}))
    timeutil.set_now(datetime(2026, 10, 22, 10, 0, tzinfo=timeutil.UTC))
    with pytest.raises(search.RepeatSearch) as e:
        run(conn, settings, edison, FakePlaces(pages={Q: [crawley_plumbers()]}))
    assert "Jack searched plumbers in Crawley 6 days ago" in str(e.value)
    assert "new lead" in str(e.value)
    # 15 days later: no warning
    timeutil.set_now(datetime(2026, 10, 31, 10, 0, tzinfo=timeutil.UTC))
    run(conn, settings, edison, FakePlaces(pages={Q: [crawley_plumbers()]}))


def test_town_geocoded_once_and_cached(conn, settings, edison):
    places = FakePlaces(pages={"plumbers in Lewes, UK": [[]]}, town_results={"Lewes": {"latitude": 50.87, "longitude": 0.01}})
    run(conn, settings, edison, places, town="Lewes", max_leads=1, request_cap=1)
    geo = [c for c in places.calls if "nextPageToken" not in c["field_mask"]]
    assert len(geo) == 1
    run(conn, settings, edison, places, town="Lewes", max_leads=1, request_cap=1, force=True)
    geo = [c for c in places.calls if "nextPageToken" not in c["field_mask"]]
    assert len(geo) == 1  # cached


def test_unknown_town_stops_without_more_requests(conn, settings, edison):
    places = FakePlaces(town_results={})
    with pytest.raises(search.TownNotFound):
        run(conn, settings, edison, places, town="Nowhereville")
    assert len(places.calls) == 1
    with pytest.raises(search.TownNotFound):
        run(conn, settings, edison, places, town="Nowhereville")
    assert len(places.calls) == 1  # not-found result is cached too


# ---------------------------------------------------------------------------
# Free tier
# ---------------------------------------------------------------------------

def _fill_usage(conn, sku, n, ts):
    conn.executemany("INSERT INTO api_usage(sku, ts) VALUES (?, ?)", [(sku, ts)] * n)
    conn.commit()


def test_free_tier_refuses_at_safety_limit(conn, settings, edison):
    _fill_usage(conn, TEXT_SEARCH, 899, "2026-10-05 12:00:00")
    guard = UsageGuard(conn, settings)
    guard.check(TEXT_SEARCH)  # 900th is allowed
    _fill_usage(conn, TEXT_SEARCH, 1, "2026-10-05 12:00:00")
    with pytest.raises(AllowanceReached) as e:
        guard.check(TEXT_SEARCH)
    assert str(e.value) == "Free monthly Google allowance reached for Text Search (900/1000). Resets on 01 November 2026."


def test_search_stops_cleanly_at_allowance_and_keeps_results(conn, settings, edison):
    _fill_usage(conn, TEXT_SEARCH, 899, "2026-10-05 12:00:00")
    page1 = [place(f"a{i}", f"Plumber {i}", f"01293 4000{i:02d}") for i in range(5)]
    page2 = [place(f"b{i}", f"Heating {i}", f"01293 5000{i:02d}") for i in range(5)]
    places = FakePlaces(pages={Q: [page1, page2]})
    rep = run(conn, settings, edison, places, max_leads=30)
    assert len(places.calls) == 1
    assert rep.stop_reason == "allowance"
    assert "Free monthly Google allowance reached for Text Search (900/1000)" in rep.stop_message
    assert rep.qualified == 5
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 5


def test_free_tier_resets_on_first_pacific(conn, settings):
    # Usage at the end of October (Pacific)...
    _fill_usage(conn, TEXT_SEARCH, 900, "2026-10-31 20:00:00")  # 13:00 Pacific, 31 Oct
    timeutil.set_now(datetime(2026, 11, 1, 6, 0, tzinfo=timeutil.UTC))  # still 31 Oct 23:00 Pacific
    guard = UsageGuard(conn, settings)
    with pytest.raises(AllowanceReached):
        guard.check(TEXT_SEARCH)
    timeutil.set_now(datetime(2026, 11, 1, 8, 0, tzinfo=timeutil.UTC))  # 1 Nov 00:00 PDT->PST: new month
    guard.check(TEXT_SEARCH)
    assert guard.used_this_month(TEXT_SEARCH) == 0


def test_free_tier_skus_are_separate(conn, settings):
    _fill_usage(conn, TEXT_SEARCH, 900, "2026-10-05 12:00:00")
    guard = UsageGuard(conn, settings)
    guard.check(PLACE_DETAILS)  # allowances don't pool


def test_free_tier_off_uses_budget(conn, home):
    from leadfinder.config import load_settings

    (home / ".env").write_text("FREE_TIER_MODE=false\nMONTHLY_API_BUDGET_GBP=0.05\n", encoding="utf-8")
    s = load_settings(home)
    guard = UsageGuard(conn, s)
    _fill_usage(conn, TEXT_SEARCH, 1500, "2026-10-05 12:00:00")
    guard.check(TEXT_SEARCH)  # usage rows carry no cost; budget not yet spent
    conn.execute("INSERT INTO api_usage(sku, ts, est_cost_gbp) VALUES (?, ?, ?)", (TEXT_SEARCH, "2026-10-06 00:00:00", 0.04))
    conn.commit()
    with pytest.raises(AllowanceReached):
        guard.check(TEXT_SEARCH)


def test_usage_logged_per_request(conn, settings, edison):
    rep = run(conn, settings, edison, FakePlaces(pages={Q: [crawley_plumbers()]}), max_leads=3)
    rows = conn.execute("SELECT sku, user_id, search_id FROM api_usage").fetchall()
    assert len(rows) == rep.requests_made == 1
    assert rows[0]["sku"] == TEXT_SEARCH and rows[0]["user_id"] == edison.id and rows[0]["search_id"] == rep.search_id


def test_search_history_recorded(conn, settings, edison):
    rep = run(conn, settings, edison, FakePlaces(pages={Q: [crawley_plumbers()]}))
    s = conn.execute("SELECT * FROM searches WHERE id=?", (rep.search_id,)).fetchone()
    assert s["user_id"] == edison.id and s["trade"] == "plumbers" and s["town"] == "Crawley"
    assert s["qualified"] == rep.qualified and s["raw_results"] == 12 and s["status"] == "done"
    assert Q in s["queries_json"]
    owners = {r[0] for r in conn.execute("SELECT owner_id FROM leads WHERE duplicate_of IS NULL AND status='new'")}
    assert owners == {edison.id}


# ---------------------------------------------------------------------------
# Phones
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,kind,e164", [
    ("07700 900123", "mobile", "+447700900123"),
    ("+44 7700 900123", "mobile", "+447700900123"),
    ("01293 123456", "landline", "+441293123456"),
    ("+44 (0)1293 123456", "landline", "+441293123456"),
    ("020 7946 0000", "landline", "+442079460000"),
    ("0300 123 4567", "non_geographic", "+443001234567"),
    ("0800 123 4567", "non_geographic", "+448001234567"),
    ("0845 123456", "non_geographic", "+44845123456"),
    ("12345", "invalid", ""),
    ("07700 9001", "invalid", ""),
    ("", "none", ""),
])
def test_uk_phone_classification(raw, kind, e164):
    info = phones.classify(raw)
    assert info.kind == kind
    assert info.e164 == e164
