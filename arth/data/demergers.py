"""
Demergers in the paper ledger.

On a demerger the parent keeps its share count and drops in price; its holders receive shares of the
resulting company, which usually lists one to three months later. Until then a real demat account holds
shares that cannot be priced or sold. The paper ledger mirrors that:

* On the ex-date each holding's entitlement is booked as a pending holding, valued at what the market took
  out of the parent price: parent shares x (previous close - ex-date special pre-open price). The value is
  fixed until listing (an estimate, shown as such on the dashboard).
* Cost is split as the company's cost-apportionment notice says (config/demergers.yaml), else in proportion
  to the ex-date prices. The resulting shares keep the parent's purchase dates, as Indian tax law does.
* When the resulting company's symbol (from the config) appears in NSE's price file, the pending holding
  becomes an ordinary holding at market price. It is not a target of the strategy, so the next monthly
  rebalance sells it, as any non-target holding is sold.
"""
from __future__ import annotations
import datetime as dt
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
TERMS = ROOT / "config/demergers.yaml"


def key(parent: str, ex) -> str:
    return f"DM:{parent}@{pd.Timestamp(ex).date()}"


def load_terms(path: Path = TERMS) -> dict:
    if not path.exists():
        return {}
    import yaml
    return yaml.safe_load(path.read_text()) or {}


def terms_for(terms: dict, symbol: str, ex) -> dict:
    return terms.get(f"{symbol}@{pd.Timestamp(ex).date()}") or {}


def book(book, pos: dict, parent: str, ex, prev_close: float, ex_price: float, t: dict) -> dict | None:
    """Move part of the parent's lots (those bought before the ex-date) into a pending entitlement.
    Returns the entitlement record, or None if nothing was held before the ex-date."""
    ex = pd.Timestamp(ex).date()
    lots = [l for l in book.lots[parent] if l.date < ex]
    held = sum(l.qty for l in lots)
    if held <= 0 or not (prev_close > ex_price > 0):
        return None
    pct = t.get("parent_cost_pct")
    keep = float(pct) / 100 if pct is not None else ex_price / prev_close
    ratio = t.get("ratio")
    r = float(ratio) if ratio else 1.0
    new_lots, moved = [], 0.0
    for l in lots:
        m = l.unit_cost * (1 - keep)
        moved += m * l.qty
        new_lots.append([l.qty * r, m / r, l.date.isoformat()])
        l.unit_cost *= keep
    return {"key": key(parent, ex), "parent": parent, "ex": ex.isoformat(), "parent_qty": held,
            "name": t.get("name"), "symbol": t.get("symbol"), "ratio": float(ratio) if ratio else None,
            "qty": held * r if ratio else None, "value": held * (prev_close - ex_price),
            "per_parent_share": prev_close - ex_price, "cost": moved, "lots": new_lots,
            "parent_cost_pct": keep * 100, "terms": bool(t)}


def pending_value(spins: list) -> float:
    return float(sum(s["value"] for s in spins or []))


def convert_listed(spins: list, book, pos: dict, raw: pd.DataFrame, t: pd.Timestamp, log) -> list:
    """Turn pending entitlements whose resulting company now trades into ordinary holdings."""
    from arth.ledger.lots import Lot
    keep = []
    for s in spins or []:
        sym = s.get("symbol")
        if not (sym and s.get("qty") and sym in raw.columns):
            keep.append(s); continue
        seen = raw.loc[pd.Timestamp(s["ex"]):t, sym].dropna()
        if seen.empty:
            keep.append(s); continue
        q = int(round(s["qty"]))
        pos[sym] = pos.get(sym, 0) + q
        for lq, uc, d in s["lots"]:
            book.lots[sym].append(Lot(int(round(lq)), float(uc), dt.date.fromisoformat(d)))
        log("info", f"{sym} listed: {q} shares from the {s['parent']} demerger are now a holding "
                    f"(est. value was {s['value']:,.0f}, market value {q * float(seen.iloc[-1]):,.0f})")
    return keep
