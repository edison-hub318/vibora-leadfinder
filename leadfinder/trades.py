"""Trades (from trades.yaml), chain names (chains.txt) and name matching helpers."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .config import Settings, load_chains, load_yaml, save_yaml

FAMILIES = ("trades", "beauty", "food", "professional", "generic")

_STOPWORDS = {"ltd", "limited", "llp", "plc", "co", "company", "the", "and", "uk", "services", "service", "&"}


@dataclass
class Trade:
    key: str
    label: str
    family: str = "generic"
    synonyms: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    known: bool = True  # False when typed by the user and not in trades.yaml

    @property
    def query_text(self) -> str:
        return self.label.lower() if self.known else self.key


def normalise_text(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def name_key(name: str | None) -> str:
    """Name used to spot the same business across listings/towns ('J. Smith Plumbing Ltd' -> 'j smith plumbing')."""
    words = [w for w in normalise_text(name).split() if w not in _STOPWORDS]
    return " ".join(words)


def slug_key(text: str) -> str:
    return normalise_text(text).replace(" ", "_")


def load_trades(settings: Settings) -> dict[str, Trade]:
    data: dict[str, Any] = load_yaml(settings.config_file("trades.yaml"))
    out: dict[str, Trade] = {}
    for key, v in (data or {}).items():
        v = v or {}
        out[key] = Trade(
            key=key,
            label=v.get("label", key.replace("_", " ").title()),
            family=v.get("family", "generic") if v.get("family") in FAMILIES else "generic",
            synonyms=list(v.get("synonyms", []) or []),
            types=list(v.get("types", []) or []),
            keywords=[k.lower() for k in (v.get("keywords", []) or [])],
            services=list(v.get("services", []) or []),
        )
    return out


def resolve_trade(settings: Settings, text: str) -> Trade:
    """Find a trade by key, label or synonym; otherwise return an 'unknown' trade for free text."""
    trades = load_trades(settings)
    t = normalise_text(text)
    for trade in trades.values():
        candidates = {normalise_text(trade.key.replace("_", " ")), normalise_text(trade.label)}
        candidates |= {normalise_text(s) for s in trade.synonyms}
        # allow singular forms: "plumber" for "plumbers"
        candidates |= {c[:-1] for c in list(candidates) if c.endswith("s")}
        if t in candidates or (t.endswith("s") and t[:-1] in candidates):
            return trade
    return Trade(key=text.strip().lower(), label=text.strip(), known=False)


def save_new_trade(settings: Settings, text: str, family: str = "generic") -> Trade:
    """Add a free-text trade to trades.yaml (in the project home)."""
    path = settings.home / "trades.yaml"
    if not path.exists():
        path.write_text(settings.config_file("trades.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    data = load_yaml(path)
    key = slug_key(text)
    if key not in data:
        words = [w for w in normalise_text(text).split() if len(w) > 3]
        data[key] = {
            "label": text.strip().capitalize(),
            "family": family if family in FAMILIES else "generic",
            "synonyms": [],
            "types": [],
            "keywords": [w.rstrip("s") for w in words] or [normalise_text(text)],
            "services": [],
        }
        save_yaml(path, data)
    return resolve_trade(settings, text)


def trade_matches(trade: Trade, name: str | None, primary_type: str | None) -> bool:
    """Category check: does this listing fit the trade? Unknown trades always match."""
    if not trade.known:
        return True
    if primary_type and primary_type in trade.types:
        return True
    n = normalise_text(name)
    if any(k in n for k in trade.keywords):
        return True
    # No primary type at all: give the benefit of the doubt (small businesses often lack one).
    return not primary_type


class ChainMatcher:
    def __init__(self, names: list[str]):
        self.patterns = []
        for n in names:
            norm = normalise_text(n)
            if norm:
                self.patterns.append((n, re.compile(r"(?:^| )" + re.escape(norm) + r"(?: |$)")))

    @classmethod
    def from_settings(cls, settings: Settings) -> "ChainMatcher":
        return cls(load_chains(settings))

    def match(self, name: str | None) -> str | None:
        n = normalise_text(name)
        for original, pat in self.patterns:
            if pat.search(n):
                return original
        return None
