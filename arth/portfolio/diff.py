"""Turn target rupee values into whole-share orders. Pure: no prices fetched, no state kept."""
from __future__ import annotations
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Order:
    symbol: str
    side: str        # BUY | SELL
    qty: int
    ref_price: float
    reason: str      # exit | trim | entry | top_up


def plan_orders(holdings: dict[str, int], target_value: dict[str, float], prices: dict[str, float],
                band: float = 0.25) -> list[Order]:
    """Sells first (exits, then trims beyond the band), then buys (entries, top-ups beyond the band)."""
    orders: list[Order] = []
    for s, q in holdings.items():
        p = prices[s]
        if s not in target_value:
            if q > 0:
                orders.append(Order(s, "SELL", q, p, "exit"))
            continue
        tgt_q = math.floor(target_value[s] / p)
        diff = q - tgt_q
        if diff > 0 and diff * p > band * target_value[s]:
            orders.append(Order(s, "SELL", diff, p, "trim"))
    for s, w in target_value.items():
        p = prices.get(s)
        if not (p and p > 0):
            continue
        tgt_q = math.floor(w / p)
        cur = holdings.get(s, 0)
        diff = tgt_q - cur
        if diff > 0 and (cur == 0 or diff * p > band * w):
            orders.append(Order(s, "BUY", diff, p, "entry" if cur == 0 else "top_up"))
    return orders
