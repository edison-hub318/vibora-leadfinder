"""Fake external clients with realistic fixture data. No network in tests."""
from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path
from typing import Any

from leadfinder.clients import Clients, ExternalError, FetchResult

CRAWLEY = {"latitude": 51.1092, "longitude": -0.1872}


def place(
    pid: str,
    name: str,
    phone: str | None = "01293 555 0101",
    website: str | None = None,
    rating: float | None = 4.7,
    reviews: int = 48,
    status: str = "OPERATIONAL",
    ptype: str | None = "plumber",
    lat: float = 51.112,
    lng: float = -0.19,
    address: str = "12 High Street, Crawley RH10 1AA, UK",
) -> dict[str, Any]:
    p: dict[str, Any] = {
        "id": pid,
        "displayName": {"text": name, "languageCode": "en"},
        "formattedAddress": address,
        "businessStatus": status,
        "googleMapsUri": f"https://maps.google.com/?cid={abs(hash(pid)) % 10**12}",
        "location": {"latitude": lat, "longitude": lng},
        "userRatingCount": reviews,
    }
    if phone:
        p["nationalPhoneNumber"] = phone
    if website:
        p["websiteUri"] = website
    if rating is not None:
        p["rating"] = rating
    if ptype:
        p["primaryType"] = ptype
    return p


def crawley_plumbers() -> list[dict[str, Any]]:
    """A realistic first page for 'plumbers in Crawley'."""
    return [
        place("p1", "Smith Plumbing & Heating", "07700 900101", None, 4.8, 142, address="4 Ifield Rd, Crawley RH11 7AA, UK"),
        place("p2", "Crawley Boiler Repairs", "01293 512345", "https://www.facebook.com/crawleyboilers", 4.9, 94,
              address="Unit 3, Manor Royal, Crawley RH10 9PY, UK"),
        place("p3", "J Patel Plumbing Ltd", "01293 600700", "http://jpatelplumbing.co.uk", 4.6, 61,
              address="22 Brighton Rd, Crawley RH10 6AA, UK"),
        place("p4", "AquaFlow Heating", "07811 223344", "https://aquaflow.wixsite.com/home", 4.4, 27,
              address="9 Gossops Dr, Crawley RH11 8LH, UK"),
        place("p5", "B&Q Crawley", "0333 014 3098", "https://www.diy.com", 4.1, 2100, ptype="home_improvement_store",
              address="County Oak Way, Crawley RH11 7ST, UK"),
        place("p6", "British Gas Crawley", "0333 202 9802", "https://www.britishgas.co.uk", 2.1, 12,
              address="Crawley RH10 1BB, UK"),
        place("p7", "Victoria Plumbing London", "020 7946 0000", "https://victoriaplumbing.co.uk", 4.5, 300,
              lat=51.5074, lng=-0.1278, address="London SW1A 1AA, UK"),
        place("p8", "Old Town Plumbers (closed)", "01293 000111", None, 4.0, 5, status="CLOSED_PERMANENTLY",
              address="1 Old Town, Crawley RH11 0AA, UK"),
        place("p9", "Smith Plumbing and Heating Ltd", "07700 900101", None, 4.5, 12, address="4 Ifield Rd, Crawley RH11 7AA, UK"),
        place("p10", "Ghost Plumbing", None, None, None, 0, address="Crawley RH10 2ZZ, UK"),
        place("p11", "Three Bridges Heating", "01293 777888", "https://threebridgesheating.co.uk", 3.1, 33,
              address="Station Hill, Crawley RH10 1JA, UK"),
        place("p12", "Pound Hill Plumber", "01293 888999", None, 4.9, 15, address="Worth Rd, Crawley RH10 7DA, UK"),
    ]


class FakePlaces:
    """Scripted Places API. `pages` maps a textQuery to a list of pages (lists of places);
    pages are served in order and a nextPageToken is returned while more remain."""

    def __init__(self, pages: dict[str, list[list[dict[str, Any]]]] | None = None, default: list[list[dict]] | None = None,
                 town_results: dict[str, dict[str, float] | None] | None = None, details: dict[str, dict] | None = None):
        self.pages = pages or {}
        self.default = default if default is not None else []
        self.town_results = town_results or {}
        self.details = details or {}
        self.calls: list[dict[str, Any]] = []
        self.detail_calls: list[str] = []

    def text_search(self, body: dict[str, Any], field_mask: str) -> dict[str, Any]:
        self.calls.append({"body": body, "field_mask": field_mask})
        q = body["textQuery"]
        if field_mask.startswith("places.id,places.displayName,places.formattedAddress,places.location") and "nextPageToken" not in field_mask:
            town = q.replace(", UK", "")
            loc = self.town_results.get(town, CRAWLEY if town.lower() in ("crawley",) else None)
            if loc is None:
                return {}
            return {"places": [{"id": "town-" + town, "displayName": {"text": town}, "formattedAddress": f"{town}, UK", "location": loc}]}
        pages = self.pages.get(q, self.default)
        idx = 0
        if body.get("pageToken"):
            idx = int(body["pageToken"].split(":")[1])
        if idx >= len(pages):
            return {}
        out: dict[str, Any] = {"places": pages[idx]}
        if idx + 1 < len(pages):
            out["nextPageToken"] = f"tok:{idx + 1}"
        return out

    def place_details(self, place_id: str, field_mask: str) -> dict[str, Any]:
        self.detail_calls.append(place_id)
        if place_id not in self.details:
            raise ExternalError("not found")
        return self.details[place_id]


class FakePageSpeed:
    def __init__(self, scores: dict[str, int | None] | None = None, default: int | None = 85):
        self.scores = scores or {}
        self.default = default
        self.calls: list[str] = []

    def mobile_score(self, url: str) -> int | None:
        self.calls.append(url)
        for k, v in self.scores.items():
            if k in url:
                return v
        return self.default


GOOD_HTML = """<!doctype html><html><head><title>J Patel Plumbing | Crawley plumber</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Plumbing and heating in Crawley"></head>
<body><nav><a href="/services">Services</a> <a href="/contact">Contact us</a></nav>
<h1>J Patel Plumbing</h1><p>Call us on 01293 600700</p>
<form action="/send"><input type="email" name="email"><textarea name="message"></textarea></form>
<footer>&copy; {year} J Patel Plumbing Ltd. Email info@jpatelplumbing.co.uk</footer></body></html>"""

POOR_HTML = """<html><head><title>Three Bridges Heating</title></head>
<body><table><tr><td><table><tr><td>Welcome</td></tr></table></td></tr></table><table><tr><td>x</td></tr></table>
<a href="/old-page">Old</a><a href="/gallery">Gallery</a><a href="/prices">Prices</a><a href="/contact.html">Contact</a>
<p>Copyright 2016 Three Bridges Heating. Website by Pixel Perfect Web Design</p></body></html>"""


class FakeFetcher:
    def __init__(self, pages: dict[str, FetchResult] | None = None):
        self.pages = pages or {}
        self.calls: list[str] = []

    def _find(self, url: str) -> FetchResult:
        if url in self.pages:
            return self.pages[url]
        if url.endswith("/robots.txt"):
            return FetchResult(url=url, final_url=url, status=404, text="")
        return FetchResult(url=url, final_url=url, status=404, text="not found")

    def get(self, url: str, timeout: float = 15.0) -> FetchResult:
        self.calls.append(url)
        return self._find(url)

    def head(self, url: str, timeout: float = 10.0) -> FetchResult:
        self.calls.append("HEAD " + url)
        r = self._find(url)
        return FetchResult(url=url, final_url=r.final_url, status=r.status, error=r.error)


def ok_page(url: str, html: str, final: str | None = None) -> FetchResult:
    return FetchResult(url=url, final_url=final or url, status=200, text=html)


class FakeCompaniesHouse:
    def __init__(self, items: dict[str, list[dict[str, Any]]] | None = None):
        self.items = items or {}
        self.calls: list[str] = []

    def search(self, query: str) -> list[dict[str, Any]]:
        self.calls.append(query)
        return self.items.get(query, [])


class FakeAI:
    def __init__(self, reply: str | None = '{"dated_score": 8, "verdict": "Looks like a 2010 template.", "biggest_problem": "Dated design"}'):
        self.reply = reply
        self.calls = 0

    def judge(self, prompt: str) -> str | None:
        self.calls += 1
        return self.reply


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, msg: EmailMessage) -> None:
        self.sent.append(msg)


class FakePublisher:
    def __init__(self) -> None:
        self.deploys: list[Path] = []

    def deploy(self, folder: Path, site_id: str | None, name_hint: str) -> tuple[str, str]:
        self.deploys.append(folder)
        return site_id or "site-123", f"https://{name_hint}.netlify.app"


def fake_clients(**kw: Any) -> Clients:
    return Clients(
        places=kw.get("places") or FakePlaces(),
        pagespeed=kw.get("pagespeed") or FakePageSpeed(),
        fetcher=kw.get("fetcher") or FakeFetcher(),
        companies_house=kw.get("companies_house"),
        ai=kw.get("ai"),
        mailer=kw.get("mailer") or FakeMailer(),
        publisher=kw.get("publisher") or FakePublisher(),
    )
