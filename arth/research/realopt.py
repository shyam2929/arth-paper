"""
Monthly Nifty strangle on REAL option prices from NSE F&O bhavcopies (data/raw/fo_nifty).

Same rules as the VIX-priced simulation (bt/optsim.py) so the two can be compared:
  - enter at the close of each monthly expiry, sell the next monthly expiry's call and put at the strikes
    nearest S*exp(+/- sigma*sqrt(T)) with sigma = India VIX (strike choice only; prices are real)
  - mark daily; stop when the buy-back cost exceeds (1 + stop) x credit; margin call when losses eat the
    buffer (capital = margin / util); settle at expiry on the Nifty close
  - price used each day: the contract's close if it traded that day, else NSE's settlement price
  - costs: STT 0.15% of premium sold, NSE 0.03553% of premium (+GST), stamp 0.003% on premium bought,
    Rs 20 + GST per leg per order, `slip` rupees per option on every entry/exit
"""
from __future__ import annotations
import glob, math
from pathlib import Path
import numpy as np
import pandas as pd

RAW = "data/raw/fo_nifty"


def load(raw=RAW) -> pd.DataFrame:
    parts = []
    for f in sorted(glob.glob(f"{raw}/*.parquet")):
        d = pd.read_parquet(f); d["date"] = pd.Timestamp(Path(f).stem); parts.append(d)
    df = pd.concat(parts, ignore_index=True)
    df = df[df.kind == "OPTIDX"].copy()
    for c in ("strike", "close", "settle", "contracts", "oi"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["px"] = np.where(df.contracts.fillna(0) > 0, df.close, df.settle)
    return df[["date", "expiry", "strike", "opt", "px", "contracts", "oi"]]


def monthly_expiries(df: pd.DataFrame) -> list[pd.Timestamp]:
    """The monthly contract of each month = that month's expiry date with the most rows. (Long-dated
    contracts can keep the originally scheduled date when a holiday moves the monthly expiry.)"""
    n = df.groupby("expiry").size()
    n.index = pd.to_datetime(n.index)
    by_month = n.groupby(n.index.to_period("M")).idxmax()
    return sorted(by_month.tolist())


def cost_leg(prem, sold, lot=65):
    val = prem * lot
    c = 20 * 1.18 + val * 0.0003553 * 1.18
    c += val * 0.0015 if sold else val * 0.00003
    return c / lot


def run(df, S, V, util=0.5, stop=2.0, slip=1.5, y=0.05, width=1.0, start="2018-02-01", vix_margin=True):
    exps = [e for e in monthly_expiries(df) if e >= pd.Timestamp(start) and e <= S.index[-1]]
    days = S.index
    key = df.set_index(["date", "expiry", "strike", "opt"]).px.sort_index()
    cap = 1.0; eq = {}; rows = []
    td = lambda x: days[days <= x][-1]          # holiday expiries settle on the previous session
    for a_c, b in zip(exps[:-1], exps[1:]):
        a, bd = td(a_c), td(b)
        s0, v0 = S[a], V[a]; t0 = (bd - a).days / 365
        chain = df[(df.date == a) & (df.expiry == b)]
        if chain.empty:
            continue
        sd = s0 * v0 * math.sqrt(t0) * width
        traded = chain[chain.contracts.fillna(0) > 0]          # choose only strikes that actually traded
        calls = traded[traded.opt == "CE"]; puts = traded[traded.opt == "PE"]
        if calls.empty or puts.empty:
            continue
        kc = calls.strike.iloc[(calls.strike - (s0 + sd)).abs().argsort().iloc[0]]
        kp = puts.strike.iloc[(puts.strike - (s0 - sd)).abs().argsort().iloc[0]]
        pc = key.get((a, b, kc, "CE")); pp = key.get((a, b, kp, "PE"))
        if pc is None or pp is None or not (pc > 0 and pp > 0):
            continue
        credit = (pc - slip) + (pp - slip)
        fees = cost_leg(pc, True) + cost_leg(pp, True)
        # exchange margin (SPAN) scales with volatility; 12% of spot at a VIX of 16 or below
        margin_unit = 0.12 * s0 * (max(1.0, v0 * 100 / 16) if vix_margin else 1.0)
        units = cap / (margin_unit / util)
        start_cap = cap; reason = "expiry"; closed_on = None
        path = days[(days > a) & (days <= bd)]
        for d in path:
            carry = start_cap * y * (d - a).days / 365
            if d == bd:
                val = max(0.0, S[d] - kc) + max(0.0, kp - S[d])
                eqd = start_cap + units * (credit - val - fees) + carry
                eq[d] = eqd; break
            qc = key.get((d, b, kc, "CE")); qp = key.get((d, b, kp, "PE"))
            if qc is None or qp is None or np.isnan(qc) or np.isnan(qp):
                eq[d] = eq.get(d, start_cap + carry); continue
            val = qc + qp
            eqd = start_cap + units * (credit - val - fees) + carry
            if eqd < start_cap * util or (stop and val - credit > stop * credit):
                exit_cost = 2 * slip + cost_leg(qc, False) + cost_leg(qp, False)
                eqd = start_cap + units * (credit - val - fees - exit_cost) + carry
                reason = "margin" if eqd < start_cap * util else "stop"
                eq[d] = eqd; closed_on = d; break
            eq[d] = eqd
        if closed_on is not None:
            c = eq[closed_on]
            for d in path[path > closed_on]:
                c *= 1 + y * (d - days[days < d][-1]).days / 365; eq[d] = c
        cap = eq[path[-1]]
        rows.append(dict(entry=a, expiry=b, S=s0, vix=v0 * 100, kc=kc, kp=kp, pc=pc, pp=pp,
                         credit=credit, ret=cap / start_cap - 1, exit=reason))
    return pd.Series(eq).sort_index(), pd.DataFrame(rows)


def bs_iv(price, s, k, t, cp, r=0.065):
    """Implied vol by bisection (for comparing real premiums with VIX)."""
    from scipy.stats import norm
    lo, hi = 1e-4, 3.0
    for _ in range(80):
        v = (lo + hi) / 2
        d1 = (math.log(s / k) + (r + v * v / 2) * t) / (v * math.sqrt(t)); d2 = d1 - v * math.sqrt(t)
        p = s * norm.cdf(d1) - k * math.exp(-r * t) * norm.cdf(d2) if cp == "CE" else \
            k * math.exp(-r * t) * norm.cdf(-d2) - s * norm.cdf(-d1)
        lo, hi = (v, hi) if p < price else (lo, v)
    return (lo + hi) / 2
