"""
Phase 0 gates on the survivorship-repaired NSE panel.
  gate 1  data integrity vs Upstox candles (surviving names)
  gate 2  Arth re-run unchanged on the repaired universe (+ a harsh 50%-haircut-on-delisting bracket)
  gate 3  index calibration: the Nifty200 Momentum 30 replica, before vs after repair
  gate 4  after-tax lead over the Midcap150 Momentum 50 fund, 2018-2026 (go / no-go: 2+ points)
  gate 5  same, 2022-2026 half
  gate 6  bootstrap odds of trailing the fund over 3 years
Usage: python scripts/phase0_gates.py   (writes data/gates.json and prints a summary)
"""
from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
UPSTOX = os.environ.get("ARTH_UPSTOX_PANEL", "/home/claude/bt/panel_all.pkl")   # optional reference panel
INDICES = str(ROOT / "data/indices.pkl")
NSE = str(ROOT / "data/panel_nse.pkl")
NSE_H = str(ROOT / "data/panel_nse_h50.pkl")
OUT = {}


def build_panels():
    from arth.data import panel as PN
    if not Path(INDICES).exists():
        from arth.data.indices import load_all
        load_all("2017-01-01").to_pickle(INDICES)
    if not Path(NSE).exists():
        PN.build(str(ROOT / "data/raw/cm"), NSE, INDICES, str(ROOT / "data/raw/bc"))
    if not Path(NSE_H).exists():
        P = pd.read_pickle(NSE)
        C, RAW = P["C"].copy(), P["RAW"].copy()
        dates = C.index; end = dates[-1]; n = 0
        for e, row in P["META"].iterrows():
            i = dates.get_loc(row["last"])
            if i < len(dates) - 10:
                C.iloc[i + 1, C.columns.get_loc(e)] = C.iloc[i, C.columns.get_loc(e)] * 0.5
                RAW.iloc[i + 1, RAW.columns.get_loc(e)] = RAW.iloc[i, RAW.columns.get_loc(e)] * 0.5
                n += 1
        P2 = dict(P); P2["C"] = C; P2["RAW"] = RAW
        pd.to_pickle(P2, NSE_H)
        print("haircut applied to", n, "delisted entities")


def research(panel: str, code: str, out_csv: str = '/dev/null') -> dict:
    env = dict(os.environ, ARTH_PANEL=panel, PYTHONPATH=str(ROOT), OUT_CSV=out_csv)
    pre = "import json,sys; from arth.research.engine_research import *\n"
    r = subprocess.run([sys.executable, "-c", pre + code], env=env, capture_output=True, text=True, cwd=ROOT)
    if r.returncode:
        raise RuntimeError(r.stderr[-3000:])
    return json.loads(r.stdout.strip().splitlines()[-1])


CORE = ("r=run(N=15,scorer='ens',overlay='trend'); e=r['equity']; import os; e.to_csv(os.environ.get('OUT_CSV','/dev/null'));"
        "s=stats(r); s={k:(float(v) if v is not None else None) for k,v in s.items()};"
        "rr=run(N=15,scorer='ens'); s2=stats(rr); s['noov_CAGR']=float(s2['CAGR']); s['noov_MaxDD']=float(s2['MaxDD']);"
        "print(json.dumps(s))")
REPL = ("res=run(start='2018-01-01',N=30,top=200,freq='H',buffer=1.0,fees=False,slip=0.0,band=0.0); e=res['equity'];"
        "idx=IDX['Nifty200Momentm30'].reindex(e.index).ffill(); s1=stats(e); s2=stats(idx);"
        "c=e.resample('ME').last().pct_change().corr(idx.resample('ME').last().pct_change());"
        "print(json.dumps({'replica':float(s1['CAGR']),'index':float(s2['CAGR']),'corr':float(c)}))")


def gate1():
    U = pd.read_pickle(UPSTOX)["C"]; P = pd.read_pickle(NSE); N = P["C"]
    common = [c for c in N.columns if c in U.columns and not c.startswith("ETF_")]
    d = N.index.intersection(U.index)
    rn = N.loc[d, common].pct_change(fill_method=None); ru = U.loc[d, common].pct_change(fill_method=None)
    diff = (rn - ru).abs().stack()
    tot_n = (N.loc[d, common].ffill().iloc[-1] / N.loc[d, common].bfill().iloc[0])
    tot_u = (U.loc[d, common].ffill().iloc[-1] / U.loc[d, common].bfill().iloc[0])
    ratio = (tot_n / tot_u).replace([np.inf, -np.inf], np.nan).dropna()
    m = P["META"]
    OUT["gate1"] = dict(common_names=len(common), stock_days=int(diff.size),
                        pct_days_diff_gt_0_5pct=round(float((diff > 0.005).mean() * 100), 2),
                        pct_names_total_return_within_2pct=round(float(((ratio - 1).abs() < 0.02).mean() * 100), 1),
                        entities=len(m), delisted_before_end=int((m["last"] < N.index[-1] - pd.Timedelta(days=15)).sum()),
                        adjust_events=int(m["n_adjust"].sum()), ca_rejected=int(m["ca_rejected"].sum()))


def aftertax(panel: str, start="2018-02-01", end=None) -> dict:
    from arth.config import StrategyParams
    from arth.strategy.signals import Features
    from arth import backtest as B
    from arth.research import aftertax as AT
    P = pd.read_pickle(panel)
    cols = [c for c in P["C"].columns if not str(c).startswith("ETF_")]
    idx = P["IDX"]
    f = Features.build(P["C"][cols], P["V"][cols], idx["Nifty_500"],
                       unadjusted=P["RAW"][cols] if "RAW" in P else None)
    res = B.run(f, StrategyParams(top_n=15, overlay=True), start=start, end=end)
    e = res.equity
    at, _ = AT.after_tax_equity(e, res.lots)
    liq = AT.liquidation_tax(res.lots, f.close.loc[e.index[-1]], e.index[-1], scale=at.iloc[-1] / e.iloc[-1])
    fund = idx["NiftyM150Momntm50"].reindex(e.index).ffill()
    fund_end = AT.fund_after_tax(fund, e.iloc[0])
    out = dict(pre_tax=B.cagr(e) * 100, after_tax=AT.cagr_between(e.iloc[0], at.iloc[-1] - liq, e.index[0], e.index[-1]) * 100,
               fund_pre=B.cagr(fund) * 100, fund_after=AT.cagr_between(e.iloc[0], fund_end, e.index[0], e.index[-1]) * 100)
    out["lead_after_tax"] = out["after_tax"] - out["fund_after"]
    return {k: round(v, 2) for k, v in out.items()}, e


def bootstrap_trail(e: pd.Series, fund: pd.Series, paths=20000, block=3, n=36, seed=7) -> float:
    m = e.resample("ME").last().pct_change().dropna()
    f = fund.reindex(e.index).ffill().resample("ME").last().pct_change().dropna()
    x = pd.concat([m, f], axis=1).dropna().values
    rng = np.random.default_rng(seed); N = len(x); trail = 0
    for _ in range(paths):
        j = rng.integers(N); idx = []
        while len(idx) < n:
            idx.append(j); j = rng.integers(N) if rng.random() < 1 / block else (j + 1) % N
        a = np.prod(1 + x[idx, 0]); b = np.prod(1 + x[idx, 1])
        trail += a < b
    return trail / paths * 100


def main():
    build_panels()
    ref = Path(UPSTOX).exists()
    if ref:
        gate1(); print("gate1", OUT["gate1"], flush=True)
    for name, p in [("upstox", UPSTOX), ("nse", NSE), ("nse_h50", NSE_H)][0 if ref else 1:]:
        OUT[f"core_{name}"] = research(p, CORE, str(ROOT / f"data/core_{name}_equity.csv")); print(name, OUT[f"core_{name}"], flush=True)
    for name, p in [("upstox", UPSTOX), ("nse", NSE)][0 if ref else 1:]:
        OUT[f"replica_{name}"] = research(p, REPL); print("replica", name, OUT[f"replica_{name}"], flush=True)
    for name, p in [("nse", NSE), ("nse_h50", NSE_H)] + ([("upstox", UPSTOX)] if ref else []):
        full, e = aftertax(p)
        half, _ = aftertax(p, start="2022-01-01")
        OUT[f"aftertax_{name}"] = dict(full=full, half_2022=half)
        if name == "nse":
            idx = pd.read_pickle(INDICES)["NiftyM150Momntm50"]
            OUT["bootstrap_p_trail_fund_3y_pretax"] = round(bootstrap_trail(e, idx), 1)
        print("aftertax", name, OUT[f"aftertax_{name}"], flush=True)
    json.dump(OUT, open(ROOT / "data/gates.json", "w"), indent=1, default=float)
    print(json.dumps(OUT, indent=1, default=float))


if __name__ == "__main__":
    main()
