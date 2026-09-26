"""
Arth's signal: pure functions of price history, no I/O, no clock.

`Features` precomputes the rolling inputs once for a whole panel (backtest) or for the latest window (live);
`universe`, `rank_ensemble` and `select` read them as of a given signal date. The arithmetic deliberately
matches the research engine line for line, so the golden replay can hold the two within 0.1 pt of CAGR.
"""
from __future__ import annotations
import math
from dataclasses import dataclass
import numpy as np
import pandas as pd

TD = 252


@dataclass
class Features:
    close: pd.DataFrame        # adjusted close, forward-filled
    vol: pd.DataFrame          # annualised daily volatility over vol_window
    r126: pd.DataFrame
    r252: pd.DataFrame
    r231: pd.DataFrame         # 12-1: return from t-252 to t-21
    tv126: pd.DataFrame        # 126-day median traded value (rupees)
    age: pd.DataFrame          # sessions with a price
    index_level: pd.Series     # overlay index, forward-filled to the panel's dates
    index_ma: pd.Series
    price_filter: pd.DataFrame | None = None   # unadjusted close for the minimum-price rule (no look-ahead)

    @classmethod
    def build(cls, close_raw: pd.DataFrame, volume: pd.DataFrame, index_level: pd.Series,
              vol_window: int = 252, ma_days: int = 200, unadjusted: pd.DataFrame | None = None) -> "Features":
        c = close_raw.ffill()
        ret = c.pct_change(fill_method=None)
        vol = ret.rolling(vol_window, min_periods=200).std() * math.sqrt(TD)
        tv = (close_raw * volume).rolling(126, min_periods=100).median()
        idx = index_level.reindex(c.index).ffill()
        return cls(close=c, vol=vol, r126=c / c.shift(126) - 1, r252=c / c.shift(252) - 1,
                   r231=c.shift(21) / c.shift(252) - 1, tv126=tv, age=close_raw.notna().cumsum(),
                   index_level=idx, index_ma=idx.rolling(ma_days).mean(),
                   price_filter=None if unadjusted is None else unadjusted.reindex_like(c).ffill())


def universe(f: Features, t, size: int = 500, min_price: float = 30.0, min_history: int = 260,
             min_tv: float = 0.0) -> list[str]:
    tv = f.tv126.loc[t]
    px = f.close.loc[t] if f.price_filter is None else f.price_filter.loc[t]
    ok = (px >= min_price) & (f.age.loc[t] >= min_history) & tv.notna() \
        & f.vol.loc[t].notna() & f.r252.loc[t].notna()
    if min_tv:
        ok &= tv >= min_tv
    return list(tv[ok].sort_values(ascending=False).index[:size])


def score_nms(f: Features, t, names) -> pd.Series:
    z6 = f.r126.loc[t, names] / f.vol.loc[t, names]
    z12 = f.r252.loc[t, names] / f.vol.loc[t, names]
    s = 0.5 * ((z6 - z6.mean()) / z6.std()) + 0.5 * ((z12 - z12.mean()) / z12.std())
    return s.sort_values(ascending=False)


def score_121(f: Features, t, names) -> pd.Series:
    return (f.r231.loc[t, names] / f.vol.loc[t, names]).sort_values(ascending=False)


def rank_ensemble(f: Features, t, names) -> pd.Series:
    """Sum of the two ranks, ascending (lower is better). Same pandas ops as research for identical ties."""
    a = score_nms(f, t, names).rank(ascending=False)
    b = score_121(f, t, names).rank(ascending=False)
    return (a + b).sort_values()


def rank(f: Features, t, names, scorer: str = "ensemble") -> pd.Series:
    """Names ordered best-first. ensemble: rank(NMS) + rank(12-1); '121': 12-1 return / vol; 'nms': NMS."""
    if scorer == "ensemble":
        return rank_ensemble(f, t, names)
    if scorer == "121":
        return score_121(f, t, names)
    if scorer == "nms":
        return score_nms(f, t, names)
    raise ValueError(scorer)


def exposure(f: Features, t, overlay: bool, risk_off: float = 0.5, lev: float = 1.0) -> float:
    if overlay and not (f.index_level.loc[t] > f.index_ma.loc[t]):
        return lev * risk_off
    return lev


def select(ranked: pd.Series, holdings: list[str], top_n: int, buffer_mult: float) -> list[str]:
    """Keep holdings ranked within buffer_mult*top_n, then fill with the best new names."""
    rank = pd.Series(np.arange(1, len(ranked) + 1), index=ranked.index)
    keep = [s for s in holdings if s in rank.index and rank[s] <= buffer_mult * top_n]
    keep = sorted(keep, key=lambda s: rank[s])[:top_n]
    return keep + [s for s in ranked.index if s not in keep][: top_n - len(keep)]


def targets(f: Features, signal_date, holdings: list[str], equity: float, p) -> dict[str, float]:
    """Target rupee value per name, given equity. `p` is a StrategyParams."""
    names = universe(f, signal_date, p.universe_size, p.min_price, p.min_history, getattr(p, "min_tv", 0.0))
    ranked = rank(f, signal_date, names, p.scorer)
    chosen = select(ranked, holdings, p.top_n, p.buffer_mult)
    expo = exposure(f, signal_date, p.overlay, p.risk_off_exposure)
    per = equity * expo / p.top_n
    return {s: per for s in chosen}
