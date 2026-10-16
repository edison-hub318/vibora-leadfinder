"""Google free-tier protection.

Every billable Google Places request is logged in `api_usage`. Before each request we count
this month's usage for that SKU (the month runs on Pacific time, like Google's billing) and
refuse the request if it would pass FREE_TIER_SAFETY x the free allowance.
With FREE_TIER_MODE=false the stop is replaced by a monthly £ budget (MONTHLY_API_BUDGET_GBP).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from . import db
from .config import Settings, load_free_tier
from .timeutil import PACIFIC, now_db, now_utc, pacific_month_window, to_db

TEXT_SEARCH = "text_search_enterprise"
TEXT_SEARCH_PRO = "text_search_pro"
PLACE_DETAILS = "place_details_enterprise"


class AllowanceReached(Exception):
    """Raised instead of making a Google request that would pass the safety limit."""

    def __init__(self, message: str, sku: str):
        super().__init__(message)
        self.sku = sku


@dataclass
class SkuStatus:
    sku: str
    label: str
    used: int
    free: int
    limit: int  # safety limit in free-tier mode
    reset_at: datetime  # UTC

    @property
    def pct(self) -> float:
        return 0.0 if not self.free else 100.0 * self.used / self.free

    @property
    def level(self) -> str:
        """'ok' | 'amber' (70%+) | 'red' (90%+), for the dashboard meter."""
        if self.pct >= 90:
            return "red"
        if self.pct >= 70:
            return "amber"
        return "ok"

    @property
    def reset_date_text(self) -> str:
        return self.reset_at.astimezone(PACIFIC).strftime("%d %B %Y")


class UsageGuard:
    def __init__(self, conn: sqlite3.Connection, settings: Settings):
        self.conn = conn
        self.settings = settings
        self.cfg = load_free_tier(settings)
        self.skus = self.cfg.get("skus", {})
        self.usd_to_gbp = float(self.cfg.get("usd_to_gbp", 0.79))

    # ---- info ---------------------------------------------------------------
    def label(self, sku: str) -> str:
        return self.skus.get(sku, {}).get("label", sku)

    def free_allowance(self, sku: str) -> int:
        return int(self.skus.get(sku, {}).get("free_per_month", 0))

    def safety_limit(self, sku: str) -> int:
        return int(self.free_allowance(sku) * self.settings.free_tier_safety)

    def price_gbp(self, sku: str) -> float:
        return float(self.skus.get(sku, {}).get("usd_per_1000", 0)) / 1000.0 * self.usd_to_gbp

    @property
    def per_search_cap(self) -> int:
        return int(self.cfg.get("per_search_request_cap", 15))

    def used_this_month(self, sku: str, user_id: int | None = None) -> int:
        start, end = pacific_month_window()
        sql = "SELECT COUNT(*) FROM api_usage WHERE sku = ? AND ts >= ? AND ts < ?"
        args: list = [sku, to_db(start), to_db(end)]
        if user_id is not None:
            sql += " AND user_id = ?"
            args.append(user_id)
        return int(self.conn.execute(sql, args).fetchone()[0])

    def spent_this_month_gbp(self) -> float:
        start, end = pacific_month_window()
        row = self.conn.execute(
            "SELECT COALESCE(SUM(est_cost_gbp), 0) FROM api_usage WHERE ts >= ? AND ts < ?",
            (to_db(start), to_db(end)),
        ).fetchone()
        return float(row[0])

    def status(self, sku: str, user_id: int | None = None) -> SkuStatus:
        _, end = pacific_month_window()
        return SkuStatus(
            sku=sku,
            label=self.label(sku),
            used=self.used_this_month(sku, user_id),
            free=self.free_allowance(sku),
            limit=self.safety_limit(sku),
            reset_at=end,
        )

    def meter(self) -> list[SkuStatus]:
        return [self.status(TEXT_SEARCH), self.status(PLACE_DETAILS)]

    def remaining(self, sku: str) -> int:
        if not self.settings.free_tier_mode:
            price = self.price_gbp(sku)
            if price <= 0:
                return 10**9
            left = self.settings.monthly_api_budget_gbp - self.spent_this_month_gbp()
            return max(0, int(left / price))
        return max(0, self.safety_limit(sku) - self.used_this_month(sku))

    # ---- the gate -----------------------------------------------------------
    def check(self, sku: str) -> None:
        """Raise AllowanceReached if one more request of this SKU isn't allowed."""
        if self.settings.free_tier_mode:
            used = self.used_this_month(sku)
            if used + 1 > self.safety_limit(sku):
                st = self.status(sku)
                raise AllowanceReached(
                    f"Free monthly Google allowance reached for {self._short(sku)} "
                    f"({used}/{st.free}). Resets on {st.reset_date_text}.",
                    sku,
                )
        else:
            spent = self.spent_this_month_gbp()
            if spent + self.price_gbp(sku) > self.settings.monthly_api_budget_gbp:
                _, end = pacific_month_window()
                raise AllowanceReached(
                    f"Monthly Google budget of £{self.settings.monthly_api_budget_gbp:.2f} reached "
                    f"(£{spent:.2f} spent). Resets on {end.astimezone(PACIFIC).strftime('%d %B %Y')}.",
                    sku,
                )

    def cost_of_request(self, sku: str) -> float:
        """Estimated £ cost of one more request: £0 while inside the free allowance."""
        if self.used_this_month(sku) < self.free_allowance(sku):
            return 0.0
        return self.price_gbp(sku)

    def record(self, sku: str, user_id: int | None, search_id: int | None = None) -> float:
        cost = self.cost_of_request(sku)
        self.conn.execute(
            "INSERT INTO api_usage(sku, ts, user_id, search_id, est_cost_gbp) VALUES (?, ?, ?, ?, ?)",
            (sku, now_db(), user_id, search_id, cost),
        )
        db.commit(self.conn)
        return cost

    @staticmethod
    def _short(sku: str) -> str:
        return {TEXT_SEARCH: "Text Search", TEXT_SEARCH_PRO: "Text Search (town lookup)", PLACE_DETAILS: "Place Details"}.get(sku, sku)


def next_reset(at: datetime | None = None) -> datetime:
    _, end = pacific_month_window(at or now_utc())
    return end


def usage_by_search(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        """SELECT s.id, s.trade, s.town, s.created_at, u.initials, s.requests_made, s.est_cost_gbp, s.qualified
           FROM searches s LEFT JOIN users u ON u.id = s.user_id
           ORDER BY s.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def usage_by_user(conn: sqlite3.Connection) -> list[dict]:
    start, end = pacific_month_window()
    rows = conn.execute(
        """SELECT COALESCE(u.full_name, 'Unattributed') AS name, a.sku, COUNT(*) AS n
           FROM api_usage a LEFT JOIN users u ON u.id = a.user_id
           WHERE a.ts >= ? AND a.ts < ? GROUP BY name, a.sku ORDER BY name""",
        (to_db(start), to_db(end)),
    ).fetchall()
    return [dict(r) for r in rows]


