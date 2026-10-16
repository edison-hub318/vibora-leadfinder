"""Lead scoring. One function, `score_lead`, so it's easy to tweak.

Score is 0-100. Higher = better prospect to call first.
"""
from __future__ import annotations

from typing import Any

# Points for each website status. A missing or social-only website is the best pitch.
WEBSITE_POINTS = {
    "none": 60,
    "social_only": 60,
    "free_builder": 48,
    "poor": 45,
    None: 30,  # has a website we haven't audited yet
    "ok": 25,
    "good": 8,
}


def score_lead(lead: dict[str, Any]) -> int:
    """Score a lead dict (needs website_status, review_count, rating, phone_type, quality_flags...)."""
    website_status = lead.get("website_status")
    if website_status is None and not lead.get("website"):
        website_status = "none"
    score = WEBSITE_POINTS.get(website_status, 30)

    reviews = int(lead.get("review_count") or 0)
    rating = lead.get("rating")

    # Reviews = an established business with customers and money.
    # 20-200 is the sweet spot: busy, but not a big company with a marketing team.
    if 20 <= reviews <= 200:
        score += 20
    elif 10 <= reviews < 20:
        score += 12
    elif 200 < reviews <= 500:
        score += 10
    elif 1 <= reviews < 10:
        score += 4
    # > 500 reviews: likely too big to need us; no bonus.

    # A good rating means they care about reputation (and can show it off on a site).
    if rating is not None and reviews > 0:
        rating = float(rating)
        if rating >= 4.5:
            score += 10
        elif rating >= 4.0:
            score += 6
        elif rating >= 3.5:
            score += 2
        else:
            score -= 15  # often struggling businesses that won't invest

    # Calling is the priority, so the phone number matters.
    phone_type = lead.get("phone_type")
    if phone_type in (None, "", "none"):
        score -= 25
    elif phone_type == "invalid":
        score -= 15
    elif phone_type == "mobile":
        score += 3  # usually the owner answers

    flags = lead.get("quality_flags") or []
    if "possible_ghost" in flags:  # 0 reviews and no website
        score -= 20

    if lead.get("existing_agency"):
        score -= 8  # may be in a contract or loyal

    if (lead.get("ai_dated_score") or 0) >= 7:
        score += 5

    return max(0, min(100, int(round(score))))
