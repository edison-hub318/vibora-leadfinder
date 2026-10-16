from __future__ import annotations

import pytest

from leadfinder import audit
from leadfinder.config import load_website_rules
from leadfinder.enrich import find_email, match_company
from leadfinder.scoring import score_lead
from leadfinder.search import SearchOptions, run_search

from .fakes import (GOOD_HTML, POOR_HTML, FakeAI, FakeCompaniesHouse, FakeFetcher, FakePageSpeed, FakePlaces,
                    crawley_plumbers, fake_clients, ok_page)


@pytest.fixture
def rules(settings):
    return load_website_rules(settings)


def good_site():
    url = "http://jpatelplumbing.co.uk"
    return FakeFetcher({
        url: ok_page(url, GOOD_HTML.format(year=2026), final="https://jpatelplumbing.co.uk/"),
        "https://jpatelplumbing.co.uk/services": ok_page("https://jpatelplumbing.co.uk/services", "<html>ok</html>"),
        "https://jpatelplumbing.co.uk/contact": ok_page("https://jpatelplumbing.co.uk/contact", "<html>contact</html>"),
    })


def poor_site():
    base = "https://threebridgesheating.co.uk"
    return FakeFetcher({base: ok_page(base, POOR_HTML, final="http://threebridgesheating.co.uk/")})


def test_social_and_builder_domains(rules):
    c = fake_clients()
    assert audit.audit_website("https://www.facebook.com/crawleyboilers", c, rules).status == "social_only"
    assert audit.audit_website("https://linktr.ee/someone", c, rules).status == "social_only"
    assert audit.audit_website("https://abc.business.site", c, rules).status == "social_only"
    r = audit.audit_website("https://aquaflow.wixsite.com/home", c, rules)
    assert r.status == "free_builder" and "Wix" in r.issues[0]
    assert audit.audit_website(None, c, rules).status == "none"
    assert c.fetcher.calls == []  # no fetches for these


def test_good_site(rules):
    c = fake_clients(fetcher=good_site(), pagespeed=FakePageSpeed(default=92))
    r = audit.audit_website("http://jpatelplumbing.co.uk", c, rules)
    assert r.status == "good", r.issues
    assert r.signals["https"] and r.signals["viewport"] and r.signals["contact_form"]
    assert r.signals["copyright_year"] == 2026
    assert r.pagespeed == 92


def test_poor_site_and_agency(rules):
    c = fake_clients(fetcher=poor_site(), pagespeed=FakePageSpeed(default=31))
    r = audit.audit_website("https://threebridgesheating.co.uk", c, rules)
    assert r.status == "poor"
    joined = " | ".join(r.issues)
    assert "Site not mobile-friendly" in joined
    assert "No HTTPS" in joined
    assert "Copyright 2016" in joined
    assert "Slow on mobile (PageSpeed 31/100)" in joined
    assert "broken links" in joined  # 4 internal links all 404
    assert r.existing_agency == "Pixel Perfect Web Design"


def test_ok_with_minor_issues(rules):
    html = GOOD_HTML.format(year=2026).replace('<meta name="description" content="Plumbing and heating in Crawley">', "")
    url = "https://example-plumber.co.uk"
    f = FakeFetcher({url: ok_page(url, html),
                     url + "/services": ok_page(url + "/services", "ok"), url + "/contact": ok_page(url + "/contact", "ok")})
    r = audit.audit_website(url, fake_clients(fetcher=f, pagespeed=FakePageSpeed(default=90)), rules)
    assert r.status == "ok"
    assert r.issues == ["No meta description"]


def test_stale_copyright_is_minor_old_is_major(rules):
    assert audit.latest_copyright_year("© 2015 - 2023 Acme", 2026) == 2023
    assert audit.latest_copyright_year("Copyright (c) 2019 Bob", 2026) == 2019
    assert audit.latest_copyright_year("no year here 2011", 2026) is None


def test_failed_load_is_poor(rules):
    r = audit.audit_website("https://down.example.co.uk", fake_clients(), rules)
    assert r.status == "poor"
    assert "Website doesn't load" in r.issues


def test_parked_domain(rules):
    url = "https://parked.example.co.uk"
    html = '<html><head><meta name="viewport" content="x"><title>parked</title></head><body>This domain is for sale! Buy this domain today.</body></html>'
    r = audit.audit_website(url, fake_clients(fetcher=FakeFetcher({url: ok_page(url, html)})), rules)
    assert r.status == "poor" and "Domain parked or for sale" in r.issues


def test_robots_respected(rules):
    url = "https://private.example.co.uk"
    f = FakeFetcher({url + "/robots.txt": ok_page(url + "/robots.txt", "User-agent: *\nDisallow: /"),
                     url: ok_page(url, GOOD_HTML.format(year=2026))})
    r = audit.audit_website(url, fake_clients(fetcher=f), rules)
    assert url not in f.calls  # homepage not fetched
    assert r.signals.get("robots_blocked")


def test_hooks():
    assert audit.make_hook({"website_status": "none", "review_count": 142, "rating": 4.8}) == "142 reviews at 4.8 stars but no website"
    assert audit.make_hook({"website_status": "social_only", "website": "https://facebook.com/x", "review_count": 94, "rating": 4.8,
                            "website_issues": ["Facebook page only, no real website"]}) == "Facebook page only, 94 reviews at 4.8 stars"
    assert audit.make_hook({"website_status": "poor", "website": "x", "website_issues": [
        "Site not mobile-friendly", "Copyright 2017", "No contact form"]}) == "Site not mobile-friendly, copyright 2017, no contact form"


def test_ai_judgement_upgrades_ok_to_poor(settings, rules):
    html = GOOD_HTML.format(year=2026).replace('<meta name="description" content="Plumbing and heating in Crawley">', "")
    url = "https://example-plumber.co.uk"
    f = FakeFetcher({url: ok_page(url, html)})
    ai = FakeAI()
    c = fake_clients(fetcher=f, pagespeed=FakePageSpeed(default=90), ai=ai)
    res = audit.audit_many([{"id": 1, "name": "X", "website": url}], c, settings)
    assert ai.calls == 1
    assert res[1].status == "poor"
    assert res[1].ai_verdict == "Looks like a 2010 template."


@pytest.mark.parametrize("reply", [None, "not json", '{"dated_score": 11, "verdict": "x", "biggest_problem": "y"}',
                                   '{"verdict": "x"}'])
def test_ai_failures_are_quiet(settings, reply):
    url = "https://example-plumber.co.uk"
    html = GOOD_HTML.format(year=2026).replace('<meta name="description" content="Plumbing and heating in Crawley">', "")
    c = fake_clients(fetcher=FakeFetcher({url: ok_page(url, html)}), pagespeed=FakePageSpeed(default=90), ai=FakeAI(reply))
    res = audit.audit_many([{"id": 1, "name": "X", "website": url}], c, settings)
    assert res[1].status == "ok" and res[1].ai_verdict is None


def test_ai_capped_per_search(home, conn):
    from leadfinder.config import load_settings

    (home / ".env").write_text("ANTHROPIC_API_KEY=x\nAI_MAX_PER_SEARCH=2\n", encoding="utf-8")
    s = load_settings(home)
    ai = FakeAI()
    leads = [{"id": i, "name": f"L{i}", "website": f"https://down{i}.example.co.uk"} for i in range(5)]
    audit.audit_many(leads, fake_clients(ai=ai), s)
    assert ai.calls == 2


def test_search_runs_audit_automatically(conn, settings, edison):
    places = FakePlaces(pages={"plumbers in Crawley, UK": [crawley_plumbers()]})
    clients = fake_clients(places=places, fetcher=good_site(), pagespeed=FakePageSpeed(default=95))
    rep = run_search(conn, settings, clients, edison, SearchOptions("plumbers", "Crawley", max_leads=30), sleep=lambda s: None)
    by_name = {lead["name"]: lead for lead in rep.new_leads}
    assert by_name["Crawley Boiler Repairs"]["website_status"] == "social_only"
    assert by_name["AquaFlow Heating"]["website_status"] == "free_builder"
    assert by_name["Smith Plumbing & Heating"]["website_status"] == "none"
    assert by_name["J Patel Plumbing Ltd"]["website_status"] == "good"
    # find-emails ran as part of the search
    assert by_name["J Patel Plumbing Ltd"]["email"] == "info@jpatelplumbing.co.uk"
    row = conn.execute("SELECT website_status, hook, email FROM leads WHERE place_id='p1'").fetchone()
    assert row["website_status"] == "none" and row["hook"] == "142 reviews at 4.8 stars but no website"
    assert rep.website["none"] >= 1


def test_find_email_only_from_site(rules):
    c = fake_clients(fetcher=good_site())
    email, source = find_email("http://jpatelplumbing.co.uk", c, rules)
    assert email == "info@jpatelplumbing.co.uk"
    assert source.startswith("https://jpatelplumbing.co.uk")
    assert find_email("https://facebook.com/x", c, rules) == (None, None)
    assert find_email("https://nothing.example.co.uk", fake_clients(), rules) == (None, None)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def base(**kw):
    d = {"website_status": "none", "review_count": 50, "rating": 4.7, "phone_type": "landline", "quality_flags": []}
    d.update(kw)
    return d


def test_score_ranges_and_order():
    s_none = score_lead(base())
    s_social = score_lead(base(website_status="social_only"))
    s_builder = score_lead(base(website_status="free_builder"))
    s_poor = score_lead(base(website_status="poor"))
    s_ok = score_lead(base(website_status="ok"))
    s_good = score_lead(base(website_status="good"))
    assert s_none == s_social > s_builder > s_poor > s_ok > s_good
    assert all(0 <= s <= 100 for s in (s_none, s_good))


def test_score_reviews_rating_phone():
    assert score_lead(base(review_count=80)) > score_lead(base(review_count=5))
    assert score_lead(base(review_count=80)) > score_lead(base(review_count=900))  # big company
    assert score_lead(base(rating=3.2)) < score_lead(base(rating=4.8))
    assert score_lead(base(phone_type="none")) < score_lead(base()) - 20
    assert score_lead(base(review_count=0, rating=None, quality_flags=["possible_ghost"])) < 50
    assert score_lead(base(existing_agency="Pixel Perfect")) < score_lead(base())


def test_score_clamped():
    assert score_lead(base(phone_type="none", website_status="good", rating=1.0, quality_flags=["possible_ghost"])) == 0
    assert score_lead(base(phone_type="mobile", ai_dated_score=9)) <= 100


# ---------------------------------------------------------------------------
# Companies House
# ---------------------------------------------------------------------------

def test_companies_house_matching():
    lead = {"name": "J Patel Plumbing Ltd", "postcode": "RH10 6AA"}
    items = [{"title": "J PATEL PLUMBING LIMITED", "company_status": "active", "company_number": "01234567",
              "address": {"postal_code": "RH10 6AA"}}]
    assert match_company(lead, items) == ("limited_company", "01234567")
    assert match_company(lead, [])[0] == "likely_sole_trader"
    other_pc = [{**items[0], "address": {"postal_code": "BN1 1AA"}}]
    assert match_company(lead, other_pc)[0] == "unknown"
    two = items + [{**items[0], "company_number": "999"}]
    assert match_company(lead, two)[0] == "unknown"
    dissolved = [{**items[0], "company_status": "dissolved"}]
    assert match_company(lead, dissolved)[0] == "likely_sole_trader"


def test_classify_command(conn, edison):
    from leadfinder.enrich import classify_command

    from .helpers import add_lead

    lid = add_lead(conn, name="J Patel Plumbing Ltd", postcode="RH10 6AA")
    ch = FakeCompaniesHouse({"J Patel Plumbing Ltd": [{"title": "J PATEL PLUMBING LTD", "company_number": "1",
                                                       "address": {"postal_code": "RH10 6AA"}}]})
    classify_command(conn, fake_clients(companies_house=ch), edison.id)
    assert conn.execute("SELECT entity_type FROM leads WHERE id=?", (lid,)).fetchone()[0] == "limited_company"
    # Without a key, nothing changes
    lid2 = add_lead(conn, name="Other")
    assert classify_command(conn, fake_clients(), edison.id) == {"limited_company": 0, "likely_sole_trader": 0, "unknown": 0}
    assert conn.execute("SELECT entity_type FROM leads WHERE id=?", (lid2,)).fetchone()[0] == "unknown"
