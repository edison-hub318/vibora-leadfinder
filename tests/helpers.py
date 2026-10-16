"""Helpers to insert leads directly for tests that don't need the search pipeline."""
from __future__ import annotations

import json
from itertools import count
from typing import Any

from leadfinder import phones
from leadfinder.timeutil import now_db

_ids = count(1000)


def add_lead(conn, **kw: Any) -> int:
    n = next(_ids)
    phone = kw.pop("phone", f"01293 5{n:05d}")
    info = phones.classify(phone)
    row = {
        "place_id": kw.pop("place_id", f"place-{n}"),
        "name": kw.pop("name", f"Business {n}"),
        "trade": kw.pop("trade", "plumbers"),
        "town": kw.pop("town", "Crawley"),
        "address": kw.pop("address", "1 High St, Crawley RH10 1AA, UK"),
        "postcode": kw.pop("postcode", "RH10 1AA"),
        "phone": phone,
        "phone_e164": info.e164 or None,
        "phone_type": info.kind,
        "website": kw.pop("website", None),
        "rating": kw.pop("rating", 4.6),
        "review_count": kw.pop("review_count", 40),
        "status": kw.pop("status", "new"),
        "website_status": kw.pop("website_status", "none"),
        "website_issues": json.dumps(kw.pop("website_issues", [])),
        "score": kw.pop("score", 50),
        "entity_type": kw.pop("entity_type", "unknown"),
        "email": kw.pop("email", None),
        "hook": kw.pop("hook", "40 reviews at 4.6 stars but no website"),
        "first_seen": kw.pop("first_seen", now_db()),
        "last_refreshed": kw.pop("last_refreshed", now_db()),
    }
    row.update(kw)
    cols = ", ".join(row)
    cur = conn.execute(f"INSERT INTO leads({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    conn.commit()
    return int(cur.lastrowid)
