"""
Reconciliation (phase 2): the ledger must match what Upstox says you hold, share for share.
Any difference freezes new orders until a person resolves it (plan: Risk rules, "Reconciliation").
"""
from __future__ import annotations


def broker_positions(holdings: list[dict], symbol_of=lambda h: h.get("tradingsymbol") or h.get("trading_symbol")) -> dict[str, int]:
    """Upstox long-term holdings -> {symbol: quantity}, counting T1 (bought, not yet delivered) shares too."""
    out: dict[str, int] = {}
    for h in holdings:
        q = int(h.get("quantity", 0) or 0) + int(h.get("t1_quantity", 0) or 0)
        if q:
            out[symbol_of(h)] = out.get(symbol_of(h), 0) + q
    return out


def diff(ledger: dict[str, int], broker: dict[str, int], ignore: set[str] = frozenset()) -> dict[str, tuple[int, int]]:
    """{symbol: (ledger_qty, broker_qty)} for every mismatch. Holdings you keep outside Arth go in `ignore`."""
    out = {}
    for s in set(ledger) | set(broker):
        if s in ignore:
            continue
        a, b = int(ledger.get(s, 0)), int(broker.get(s, 0))
        if a != b:
            out[s] = (a, b)
    return out
