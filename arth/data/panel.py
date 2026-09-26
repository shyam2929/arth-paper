"""
Build a survivorship-free, split/bonus-adjusted daily price panel from NSE bhavcopies.

Entity linking. A company keeps its ISIN through a symbol change, and keeps its symbol through a face-value
split (which issues a new ISIN). So a row joins an existing entity if its symbol OR its ISIN was seen on
that entity within the last `max_gap` trading days; otherwise it starts a new entity. Symbol reuse by a
different company after a long gap therefore starts a new entity.

Adjustment. On an ex-date NSE sets the previous close to the adjusted value, so
    f_t = prev_close_t / close_{t-1}
captures splits, bonuses, rights and demerger adjustments. Ratios within +/- `tol` (rounding, ordinary
dividends are not adjusted by NSE) are ignored. Back-adjusted close: adj_t = close_t * prod_{s>t} f_s.

Output (pickle, same shape as the research engine expects):
    {'C': adjusted close (dates x entity), 'V': turnover / adjusted close, 'RAW': unadjusted close,
     'TURN': turnover in rupees, 'META': entity table}
"""
from __future__ import annotations
import glob
from pathlib import Path
import numpy as np
import pandas as pd

SERIES_RANK = {"EQ": 0, "BE": 1, "BZ": 2}


def load_raw(raw_dir: str = "data/raw/cm") -> pd.DataFrame:
    parts = []
    for f in sorted(glob.glob(f"{raw_dir}/*.parquet")):
        df = pd.read_parquet(f)
        df["date"] = pd.Timestamp(Path(f).stem)
        parts.append(df)
    df = pd.concat(parts, ignore_index=True)
    df = df[df["isin"].astype(str).str.startswith("INE")]                  # listed equity only (no ETFs, bonds)
    df["srank"] = df.series.map(SERIES_RANK)
    df = df.sort_values(["date", "symbol", "srank"]).drop_duplicates(["date", "symbol"], keep="first")
    for c in ("close", "prev_close", "turnover", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.reset_index(drop=True)


def link_entities(df: pd.DataFrame, max_gap: int = 30) -> pd.DataFrame:
    dates = pd.DatetimeIndex(df.date.unique()).sort_values()   # Timestamps on pandas 2 and 3 alike
    didx = {pd.Timestamp(d): i for i, d in enumerate(dates)}
    by_sym: dict[str, tuple[str, int]] = {}
    by_isin: dict[str, tuple[str, int]] = {}
    ent = np.empty(len(df), dtype=object)
    names: dict[str, int] = {}
    for d, g in df.groupby("date", sort=True):
        i = didx[pd.Timestamp(d)]
        for idx, sym, isin in zip(g.index, g.symbol, g["isin"]):
            e = None
            for cand in (by_sym.get(sym), by_isin.get(isin)):
                if cand and i - cand[1] <= max_gap:
                    e = cand[0]; break
            if e is None:
                n = names.get(sym, 0); names[sym] = n + 1
                e = sym if n == 0 else f"{sym}~{n}"
            ent[idx] = e
            by_sym[sym] = (e, i); by_isin[isin] = (e, i)
    df = df.copy(); df["entity"] = ent
    return df


def adjust(df: pd.DataFrame, ca: pd.DataFrame | None = None, tol: float = 0.02,
           udiff_from: str = "2024-07-08") -> pd.DataFrame:
    """Set a price factor on each ex-date and back-adjust.

    Primary source: NSE corporate-action lists (`ca`: symbol, ex, factor). A listed action is applied on the
    entity's first trading day on or after its ex-date (within 10 sessions), unless the observed price
    ratio shows it plainly did not happen (price moved <15% while the factor implies a much bigger move).
    Secondary: an ISIN change plus a standard split ratio catches splits the lists missed. (NSE's
    previous-close field is not adjusted on ex-dates, so it cannot be used.)
    """
    df = df.sort_values(["entity", "date"]).copy()
    prev_c = df.groupby("entity").close.shift(1)
    obs = df.close / prev_c
    f = pd.Series(1.0, index=df.index); src = pd.Series("", index=df.index)
    if ca is not None and len(ca):
        pos = {}
        for (sym, d), i in zip(zip(df.symbol, df.date), df.index):
            pos.setdefault(sym, []).append((d, i))
        for v in pos.values():
            v.sort()
        for sym, ex, fac in zip(ca.symbol, ca.ex, ca.factor):
            rows = pos.get(sym)
            if not rows:
                continue
            cand = [(d, i) for d, i in rows if d >= ex][:1]
            if not cand or (cand[0][0] - ex).days > 20:
                continue
            i = cand[0][1]
            o = obs.get(i)
            if o is not None and np.isfinite(o) and abs(o - 1) < 0.15 and abs(o / fac - 1) > 0.35:
                src[i] = "ca_rejected"; continue
            f[i] *= fac; src[i] = "ca"
    # Splits missing from the lists: a split issues a new ISIN, so an ISIN change on the same symbol
    # together with a price ratio within 3% of a standard split ratio is treated as a split.
    prev_isin = df.groupby("entity")["isin"].shift(1)
    nice = np.array([1/2, 1/4, 1/5, 1/10, 2/5, 1/3, 1/2.5, 1/20])
    cand = (src == "") & prev_isin.notna() & (df["isin"] != prev_isin) & np.isfinite(obs)
    for i in df.index[cand]:
        o = obs[i]
        j = np.argmin(np.abs(nice - o))
        if abs(o / nice[j] - 1) < 0.03:
            f[i] = nice[j]; src[i] = "isin_split"
    df["factor"] = f; df["factor_src"] = src
    rev = df.iloc[::-1]
    cum = rev.groupby("entity").factor.cumprod()
    after = (cum / rev.factor).iloc[::-1]
    df["adj_close"] = df.close * after.reindex(df.index)
    return df


def build(raw_dir: str = "data/raw/cm", out: str = "data/panel_nse.pkl", idx_from: str | None = None,
          bc_dir: str = "data/raw/bc"):
    from arth.data import corp_actions
    raw = load_raw(raw_dir)
    raw = link_entities(raw)
    ca = corp_actions.load(bc_dir)
    raw = adjust(raw, ca)
    # name each entity after its latest symbol (what the broker uses today); keep suffixes on clashes
    g0 = raw.sort_values("date").groupby("entity")
    last_sym = g0.symbol.last(); last_day = g0.date.max()
    seen: dict[str, int] = {}; rename = {}
    for e in last_day.sort_values(ascending=False).index:
        sym = last_sym[e]
        n = seen.get(sym, 0); seen[sym] = n + 1
        rename[e] = sym if n == 0 else f"{sym}~{n}"
    raw["entity"] = raw.entity.map(rename)
    C = raw.pivot(index="date", columns="entity", values="adj_close").sort_index()
    RAW = raw.pivot(index="date", columns="entity", values="close").sort_index()
    TURN = raw.pivot(index="date", columns="entity", values="turnover").sort_index()
    V = TURN / C
    FAC = raw.pivot(index="date", columns="entity", values="factor").sort_index()   # ex-date factors
    g = raw.groupby("entity")
    meta = pd.DataFrame({
        "first": g.date.min(), "last": g.date.max(), "n_days": g.size(),
        "symbols": g.symbol.agg(lambda s: "|".join(pd.unique(s))),
        "isins": g["isin"].agg(lambda s: "|".join(pd.unique(s))),
        "n_adjust": g.factor.agg(lambda s: int((s != 1.0).sum())),
        "ca_rejected": g.factor_src.agg(lambda s: int((s == "ca_rejected").sum())),
        "isin_splits": g.factor_src.agg(lambda s: int((s == "isin_split").sum())),
        "last_close": g.close.last(), "last_series": g.series.last()})
    panel = {"C": C, "V": V, "RAW": RAW, "TURN": TURN, "FAC": FAC, "META": meta}
    if idx_from:
        obj = pd.read_pickle(idx_from)
        panel["IDX"] = obj["IDX"] if isinstance(obj, dict) else obj
    pd.to_pickle(panel, out)
    return panel


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="data/raw/cm"); ap.add_argument("--out", default="data/panel_nse.pkl")
    ap.add_argument("--idx-from", default=None)
    a = ap.parse_args()
    p = build(a.raw, a.out, a.idx_from)
    m = p["META"]
    print("entities", len(m), "dates", p["C"].shape[0], "stopped before end", int((m["last"] < p["C"].index[-1]).sum()))
