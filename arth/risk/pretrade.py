"""
Pre-trade checks. The risk layer can only reject, shrink or halt; it never adds exposure.
Every check returns a reason string on rejection so the order log explains itself.
"""
from __future__ import annotations
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class Quote:
    ltp: float
    ts: float                    # epoch seconds of the quote


@dataclass
class Limits:
    max_order_pct_equity: float = 0.10
    max_order_vs_target: float = 1.10
    price_collar_pct: float = 0.03
    max_quote_age_s: float = 60
    max_pct_of_median_tv: float = 0.05
    max_orders_per_sec: int = 5
    max_new_orders_per_day: int = 60
    max_reprices_per_order: int = 5
    allowed_products: tuple = ("D",)


@dataclass
class RateGate:
    per_sec: int
    per_day: int
    _sec: deque = field(default_factory=deque)
    _day: int = 0

    def allow(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        while self._sec and now - self._sec[0] >= 1.0:
            self._sec.popleft()
        if len(self._sec) >= self.per_sec or self._day >= self.per_day:
            return False
        self._sec.append(now); self._day += 1
        return True


def check_order(*, symbol: str, side: str, qty: int, limit_price: float, product: str,
                target_list: set[str], target_value: float | None, equity: float,
                quote: Quote, median_tv20: float | None, limits: Limits, now: float | None = None) -> str | None:
    """Return None if the order may go, else the reason it may not."""
    now = time.time() if now is None else now
    if product not in limits.allowed_products:
        return f"product {product} not allowed"
    if side == "BUY" and symbol not in target_list:
        return "buy of a symbol not on this month's target list"
    if qty <= 0 or limit_price <= 0:
        return "non-positive quantity or price"
    value = qty * limit_price
    if side == "BUY" and value > limits.max_order_pct_equity * equity:
        return f"order value {value:,.0f} above {limits.max_order_pct_equity:.0%} of equity"
    if side == "BUY" and target_value is not None and value > limits.max_order_vs_target * target_value:
        return "order above 1.1x its target"
    if now - quote.ts > limits.max_quote_age_s:
        return "stale quote"
    if abs(limit_price / quote.ltp - 1) > limits.price_collar_pct:
        return "limit price outside the collar"
    if median_tv20 is not None and value > limits.max_pct_of_median_tv * median_tv20:
        return "order above 5% of 20-day median traded value; split it"
    return None


def relative_breaker(equity_now: float, equity_open: float, index_now: float, index_open: float,
                     equity_drop: float = 0.07, market_drop_below: float = 0.02) -> bool:
    """True = freeze. Trips when Arth falls 7%+ since 09:15 while the index is down less than 2%."""
    e = round(equity_now / equity_open - 1, 10)
    m = round(index_now / index_open - 1, 10)
    return e <= -equity_drop and m > -market_drop_below


def drawdown_action(drawdown: float, review: float = -0.30, cut: float = -0.40) -> str:
    if drawdown <= cut:
        return "cut_to_half"
    if drawdown <= review:
        return "review"
    return "none"
