"""
Strangle test on real NSE option prices, plus the momentum + strangle stack.
Writes data/strangle_real.json. Run after the F&O archive download.
"""
from __future__ import annotations
import json, math, sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from arth.research import realopt as RO

IDX = pd.read_pickle(ROOT / "data/indices.pkl")
S = IDX["Nifty_50"].dropna(); V = (IDX["India_VIX"] / 100).reindex(S.index).ffill()


def stats(e: pd.Series, tr: pd.DataFrame) -> dict:
    e = pd.concat([pd.Series([1.0], index=[e.index[0] - pd.Timedelta(days=1)]), e])
    yrs = (e.index[-1] - e.index[0]).days / 365.25
    r = tr.ret
    return dict(CAGR=round((e.iloc[-1] ** (1 / yrs) - 1) * 100, 1), MaxDD=round((e / e.cummax() - 1).min() * 100, 1),
                median=round(r.median() * 100, 2), mean=round(r.mean() * 100, 2), ge6=round((r >= 0.06).mean() * 100, 1),
                worst=round(r.min() * 100, 1), stops=int((tr.exit != "expiry").sum()), n=len(r))


def iv_ratios(df: pd.DataFrame, tr: pd.DataFrame) -> dict:
    """Implied vol of the strikes actually sold, and of the at-the-money strike, relative to India VIX."""
    rows = []
    for _, t in tr.iterrows():
        a, b = t.entry, t.expiry; T = (b - a).days / 365; s0 = t.S
        ch = df[(df.date == a) & (df.expiry == b)]
        atm = ch.strike.iloc[(ch.strike - s0).abs().argsort().iloc[0]]
        pa = ch[(ch.strike == atm) & (ch.opt == "CE")].px
        if len(pa) == 0 or not pa.iloc[0] > 0:
            continue
        rows.append(dict(put=RO.bs_iv(t.pp, s0, t.kp, T, "PE") / (t.vix / 100),
                         call=RO.bs_iv(t.pc, s0, t.kc, T, "CE") / (t.vix / 100),
                         atm=RO.bs_iv(pa.iloc[0], s0, atm, T, "CE") / (t.vix / 100)))
    x = pd.DataFrame(rows)
    return {k: round(float(x[k].median()), 3) for k in x}


def main():
    df = RO.load(str(ROOT / "data/raw/fo_nifty"))
    out = {}
    for util in (0.5, 0.8):
        e, tr = RO.run(df, S, V, util=util, stop=2.0, slip=1.5)
        out[f"real_util{util}"] = stats(e, tr)
        if util == 0.5:
            e05, tr05 = e, tr
            e0, _ = RO.run(df, S, V, util=0.5, stop=2.0, slip=1.5, y=0.0)
    out["iv_over_vix"] = iv_ratios(df, tr05)
    # stack on Arth core (repaired NSE panel) with the 50% cash-equivalent rule and slab tax on options
    arth = pd.read_csv(ROOT / "data/core_nse_equity.csv", index_col=0, parse_dates=True).iloc[:, 0]
    ra = arth.pct_change().fillna(0); rs = e0.pct_change().reindex(arth.index).fillna(0)
    cash_d = 1.05 ** (1 / 252) - 1
    def cagr(x): y = (x.index[-1] - x.index[0]).days / 365.25; return (x.iloc[-1] / x.iloc[0]) ** (1 / y) - 1
    base_pre = cagr(arth) * 100
    for k in (0.5, 1.0):
        ce = 0.5 * k / 2 / 0.9; w = 1 - ce
        base = (1 + w * ra + ce * cash_d).cumprod(); comb = (1 + w * ra + ce * cash_d + k * rs).cumprod()
        opt = (cagr(comb) - cagr(base)) * 100
        out[f"stack_k{k}"] = dict(pre_tax=round(cagr(comb) * 100, 1), option_contribution_pre_tax=round(opt, 2),
                                  option_contribution_after_tax=round(opt * (1 - 0.312), 2) if opt > 0 else round(opt, 2),
                                  maxdd=round(float((comb / comb.cummax() - 1).min() * 100), 1))
    out["arth_core_nse_pre_tax"] = round(base_pre, 1)
    tr05.to_csv(ROOT / "data/strangle_real_trades.csv", index=False)
    json.dump(out, open(ROOT / "data/strangle_real.json", "w"), indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
