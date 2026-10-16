"""Thin clients for every external service. Each one can be swapped for a fake in tests
(see tests/fakes.py). Nothing here ever prints or logs an API key."""
from __future__ import annotations

import io
import json
import smtplib
import ssl
import zipfile
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol

import httpx

from .config import Settings

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

PLACES_FIELDS = [
    "id",
    "displayName",
    "formattedAddress",
    "nationalPhoneNumber",
    "websiteUri",
    "rating",
    "userRatingCount",
    "businessStatus",
    "primaryType",
    "googleMapsUri",
    # location is needed for the out-of-area filter; it is a Pro-tier field, so it does
    # not raise the price of an Enterprise-tier request.
    "location",
]
TEXT_SEARCH_FIELD_MASK = ",".join(f"places.{f}" for f in PLACES_FIELDS) + ",nextPageToken"
DETAILS_FIELD_MASK = ",".join(PLACES_FIELDS)
GEOCODE_FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.location"


class ExternalError(Exception):
    """An external service failed. The message never contains credentials."""


def _redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


# ---------------------------------------------------------------------------
# Google Places API (New)
# ---------------------------------------------------------------------------

class PlacesAPI(Protocol):
    def text_search(self, body: dict[str, Any], field_mask: str) -> dict[str, Any]: ...
    def place_details(self, place_id: str, field_mask: str) -> dict[str, Any]: ...


class GooglePlacesClient:
    SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
    DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

    def __init__(self, api_key: str, timeout: float = 30.0):
        self._key = api_key
        self._http = httpx.Client(timeout=timeout)

    def _headers(self, field_mask: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._key,
            "X-Goog-FieldMask": field_mask,
        }

    def _check(self, r: httpx.Response) -> dict[str, Any]:
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", r.text[:300])
            except ValueError:
                msg = r.text[:300]
            raise ExternalError(_redact(f"Google Places error {r.status_code}: {msg}", self._key))
        return r.json()

    def text_search(self, body: dict[str, Any], field_mask: str) -> dict[str, Any]:
        if not self._key:
            raise ExternalError("GOOGLE_API_KEY is not set in .env")
        try:
            r = self._http.post(self.SEARCH_URL, headers=self._headers(field_mask), json=body)
        except httpx.HTTPError as e:
            raise ExternalError(_redact(f"Could not reach Google Places: {type(e).__name__}", self._key)) from None
        return self._check(r)

    def place_details(self, place_id: str, field_mask: str) -> dict[str, Any]:
        if not self._key:
            raise ExternalError("GOOGLE_API_KEY is not set in .env")
        try:
            r = self._http.get(self.DETAILS_URL.format(place_id=place_id), headers=self._headers(field_mask))
        except httpx.HTTPError as e:
            raise ExternalError(f"Could not reach Google Places: {type(e).__name__}") from None
        return self._check(r)


# ---------------------------------------------------------------------------
# PageSpeed Insights (free)
# ---------------------------------------------------------------------------

class PageSpeedAPI(Protocol):
    def mobile_score(self, url: str) -> int | None: ...


class PageSpeedClient:
    URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

    def __init__(self, api_key: str, timeout: float = 90.0):
        self._key = api_key
        self._http = httpx.Client(timeout=timeout)

    def mobile_score(self, url: str) -> int | None:
        params = {"url": url, "strategy": "mobile", "category": "performance"}
        if self._key:
            params["key"] = self._key
        try:
            r = self._http.get(self.URL, params=params)
            if r.status_code >= 400:
                return None
            score = r.json()["lighthouseResult"]["categories"]["performance"]["score"]
            return None if score is None else int(round(float(score) * 100))
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return None  # the URL contains the key, so never surface the exception text


# ---------------------------------------------------------------------------
# Plain web fetches (homepage, contact page, link checks, robots.txt)
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int = 0
    text: str = ""
    error: str | None = None  # None | 'ssl' | 'timeout' | 'connect' | 'other'
    redirects: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 400


class Fetcher(Protocol):
    def get(self, url: str, timeout: float = 15.0) -> FetchResult: ...
    def head(self, url: str, timeout: float = 10.0) -> FetchResult: ...


class WebFetcher:
    MAX_BYTES = 2_000_000

    def __init__(self) -> None:
        self._http = httpx.Client(
            follow_redirects=True,
            headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-GB,en;q=0.9"},
            max_redirects=8,
        )

    def _do(self, method: str, url: str, timeout: float) -> FetchResult:
        try:
            r = self._http.request(method, url, timeout=timeout)
            text = ""
            if method == "GET":
                text = r.content[: self.MAX_BYTES].decode(r.encoding or "utf-8", errors="replace")
            return FetchResult(
                url=url,
                final_url=str(r.url),
                status=r.status_code,
                text=text,
                redirects=[str(h.url) for h in r.history],
            )
        except httpx.ConnectError as e:
            msg = str(e).lower()
            kind = "ssl" if ("ssl" in msg or "certificate" in msg) else "connect"
            return FetchResult(url=url, error=kind)
        except httpx.TimeoutException:
            return FetchResult(url=url, error="timeout")
        except ssl.SSLError:
            return FetchResult(url=url, error="ssl")
        except (httpx.HTTPError, ValueError):
            return FetchResult(url=url, error="other")

    def get(self, url: str, timeout: float = 15.0) -> FetchResult:
        return self._do("GET", url, timeout)

    def head(self, url: str, timeout: float = 10.0) -> FetchResult:
        res = self._do("HEAD", url, timeout)
        if res.error is None and res.status in (405, 403, 501):
            res = self._do("GET", url, timeout)
        return res


# ---------------------------------------------------------------------------
# Companies House (free)
# ---------------------------------------------------------------------------

class CompaniesHouseAPI(Protocol):
    def search(self, query: str) -> list[dict[str, Any]]: ...


class CompaniesHouseClient:
    URL = "https://api.company-information.service.gov.uk/search/companies"

    def __init__(self, api_key: str, timeout: float = 20.0):
        self._key = api_key
        self._http = httpx.Client(timeout=timeout, auth=(api_key, ""))

    def search(self, query: str) -> list[dict[str, Any]]:
        try:
            r = self._http.get(self.URL, params={"q": query, "items_per_page": 20})
        except httpx.HTTPError as e:
            raise ExternalError(f"Could not reach Companies House: {type(e).__name__}") from None
        if r.status_code >= 400:
            raise ExternalError(f"Companies House error {r.status_code}")
        return list(r.json().get("items", []) or [])


# ---------------------------------------------------------------------------
# Anthropic Messages API (optional AI website judgement)
# ---------------------------------------------------------------------------

AI_SCHEMA = {
    "type": "object",
    "properties": {
        "dated_score": {"type": "integer"},
        "verdict": {"type": "string"},
        "biggest_problem": {"type": "string"},
    },
    "required": ["dated_score", "verdict", "biggest_problem"],
    "additionalProperties": False,
}


class AIJudge(Protocol):
    def judge(self, prompt: str) -> str | None: ...


class AnthropicClient:
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, model: str, timeout: float = 60.0):
        self._key = api_key
        self.model = model
        self._http = httpx.Client(timeout=timeout)

    def judge(self, prompt: str) -> str | None:
        """Return the raw JSON text from the model, or None on any failure."""
        body = {
            "model": self.model,
            "max_tokens": 2000,
            "output_config": {
                "effort": "low",
                "format": {"type": "json_schema", "schema": AI_SCHEMA},
            },
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": self._key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        try:
            r = self._http.post(self.URL, headers=headers, json=body)
            if r.status_code >= 400:
                return None
            data = r.json()
            if data.get("stop_reason") in ("refusal", "max_tokens"):
                return None
            texts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
            return "".join(texts) or None
        except (httpx.HTTPError, ValueError):
            return None


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------

class MailSender(Protocol):
    def send(self, msg: EmailMessage) -> None: ...


class SmtpMailer:
    def __init__(self, settings: Settings):
        self.s = settings

    def send(self, msg: EmailMessage) -> None:
        s = self.s
        if not s.smtp_configured:
            raise ExternalError("SMTP is not configured in .env (SMTP_HOST and FROM_EMAIL)")
        try:
            if s.smtp_port == 465:
                with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ssl.create_default_context(), timeout=30) as smtp:
                    if s.smtp_user:
                        smtp.login(s.smtp_user, s.smtp_password)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
                    smtp.starttls(context=ssl.create_default_context())
                    if s.smtp_user:
                        smtp.login(s.smtp_user, s.smtp_password)
                    smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as e:
            raise ExternalError(_redact(f"SMTP send failed: {type(e).__name__}: {e}", s.smtp_password)) from None


# ---------------------------------------------------------------------------
# Netlify (optional preview publishing)
# ---------------------------------------------------------------------------

class Publisher(Protocol):
    def deploy(self, folder: Path, site_id: str | None, name_hint: str) -> tuple[str, str]: ...


class NetlifyClient:
    API = "https://api.netlify.com/api/v1"

    def __init__(self, token: str, timeout: float = 60.0):
        self._token = token
        self._http = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {token}"})

    def deploy(self, folder: Path, site_id: str | None, name_hint: str) -> tuple[str, str]:
        """Zip the folder and deploy it. Returns (site_id, public https url)."""
        try:
            if not site_id:
                r = self._http.post(f"{self.API}/sites", json={})
                if r.status_code >= 400:
                    raise ExternalError(f"Netlify could not create a site ({r.status_code})")
                site_id = r.json()["id"]
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for p in Path(folder).rglob("*"):
                    if p.is_file():
                        z.write(p, p.relative_to(folder).as_posix())
            r = self._http.post(
                f"{self.API}/sites/{site_id}/deploys",
                content=buf.getvalue(),
                headers={"Content-Type": "application/zip"},
            )
            if r.status_code >= 400:
                raise ExternalError(f"Netlify deploy failed ({r.status_code})")
            data = r.json()
            url = data.get("ssl_url") or data.get("url") or ""
            if not url:
                site = self._http.get(f"{self.API}/sites/{site_id}").json()
                url = site.get("ssl_url") or site.get("url", "")
            return site_id, url
        except httpx.HTTPError as e:
            raise ExternalError(f"Could not reach Netlify: {type(e).__name__}") from None


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------

@dataclass
class Clients:
    places: PlacesAPI
    pagespeed: PageSpeedAPI
    fetcher: Fetcher
    companies_house: CompaniesHouseAPI | None = None
    ai: AIJudge | None = None
    mailer: MailSender | None = None
    publisher: Publisher | None = None

    @classmethod
    def real(cls, settings: Settings) -> "Clients":
        return cls(
            places=GooglePlacesClient(settings.google_api_key),
            pagespeed=PageSpeedClient(settings.google_api_key),
            fetcher=WebFetcher(),
            companies_house=CompaniesHouseClient(settings.companies_house_api_key) if settings.companies_house_enabled else None,
            ai=AnthropicClient(settings.anthropic_api_key, settings.ai_model) if settings.ai_enabled else None,
            mailer=SmtpMailer(settings),
            publisher=NetlifyClient(settings.netlify_auth_token) if settings.netlify_auth_token else None,
        )


def json_loads_safe(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
