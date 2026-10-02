"""
Stock analyser for the static site: a price-and-volume fact sheet for every NSE share, rebuilt on each nightly run from
the files the desk has already downloaded. No extra market data is fetched per stock, so a search on the page is instant.

    build(panel, cm_dir, out_dir, db=None)  ->  OUT/a/index.json, OUT/a/cal.json, OUT/a/s/<KEY>.json, OUT/analyse.html

What it is: trend, momentum, levels, volatility, liquidity and tradability, with a mechanical reading in plain words.
What it is not: it knows nothing about the business, results, valuation or governance. The page says so.
"""
from __future__ import annotations
import datetime as dt, html, io, json, math, re, sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from arth.strategy import signals as S

ROOT = Path(__file__).resolve().parents[2]
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SERIES = ("EQ", "BE", "BZ")
CHART_DAYS = 250
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Referer": "https://www.nseindia.com/"}


def _key(sym: str) -> str:
    return re.sub(r"[^A-Z0-9_-]", "_", sym.upper())


def _r(x, nd=2):
    """Round for JSON; NaN and inf become None."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(x) else round(x, nd)


# ---- small side files (each optional: the analyser still builds without them) --------------------------------
def _sec_list(cache: Path) -> pd.DataFrame:
    """NSE's list of securities with the daily price band. Cached; a failed download falls back to the cache."""
    try:
        import requests
        for i in range(4):
            r = requests.get("https://nsearchives.nseindia.com/content/equities/sec_list.csv", headers=UA, timeout=30)
            if r.status_code == 200 and "Symbol" in r.text[:200]:
                cache.parent.mkdir(parents=True, exist_ok=True); cache.write_text(r.text); break
    except Exception:
        pass
    if not cache.exists():
        return pd.DataFrame(columns=["Symbol", "Series", "Security Name", "Band"])
    df = pd.read_csv(cache)
    df.columns = [c.strip() for c in df.columns]
    for c in ("Symbol", "Series", "Security Name", "Band"):
        df[c] = df[c].astype(str).str.strip()
    return df


def _delivery(dates: list, cache_dir: Path, budget: float = 60.0) -> pd.Series:
    """Average delivery % per symbol over the given sessions (NSE's full bhavcopy). Empty if NSE is unreachable."""
    import time
    t0 = time.time(); frames = []
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        import requests
    except Exception:
        return pd.Series(dtype=float)
    for d in dates:
        p = cache_dir / f"{d:%Y%m%d}.csv"
        if not p.exists() and time.time() - t0 < budget:
            try:
                r = requests.get(f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv",
                                 headers=UA, timeout=30)
                if r.status_code == 200 and "DELIV_PER" in r.text[:400]:
                    p.write_text(r.text)
            except Exception:
                pass
        if p.exists():
            try:
                df = pd.read_csv(p, skipinitialspace=True)
                df.columns = [c.strip() for c in df.columns]
                df = df[df.SERIES.astype(str).str.strip().isin(SERIES)]
                frames.append(pd.DataFrame({"symbol": df.SYMBOL.astype(str).str.strip(),
                                            "d": pd.to_numeric(df.DELIV_PER, errors="coerce")}))
            except Exception:
                pass
    for old in sorted(cache_dir.glob("*.csv"))[:-15]:      # keep the cache small
        old.unlink()
    if not frames:
        return pd.Series(dtype=float)
    return pd.concat(frames).dropna().groupby("symbol").d.mean()


def _mcap(raw_dir: Path) -> pd.DataFrame:
    files = sorted((raw_dir / "mcap").glob("*.parquet")) if (raw_dir / "mcap").exists() else []
    if not files:
        return pd.DataFrame(columns=["name", "mcap"])
    df = pd.read_parquet(files[-1])
    df.columns = [c.strip() for c in df.columns]
    out = pd.DataFrame({"symbol": df["Symbol"].astype(str).str.strip(), "name": df["Security Name"].astype(str).str.strip(),
                        "mcap": pd.to_numeric(df["Market Cap(Rs.)"], errors="coerce")})
    return out.drop_duplicates("symbol").set_index("symbol")


# ---- indicators (wide frames: one column per stock) -----------------------------------------------------------
def _wilder(df: pd.DataFrame, n: int) -> pd.DataFrame:
    return df.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def _pivots(h: pd.Series, l: pd.Series, last: float, k: int = 5):
    """Swing highs above and swing lows below the last price: a bar that is the extreme of the k bars either side."""
    hi = h[(h == h.rolling(2 * k + 1, center=True).max())]
    lo = l[(l == l.rolling(2 * k + 1, center=True).min())]
    res = sorted({round(float(x), 2) for x in hi if x > last * 1.005})[:3]
    sup = sorted({round(float(x), 2) for x in lo if x < last * 0.995}, reverse=True)[:3]
    return sup, res


def _inr(x: float) -> str:
    return f"₹{x:,.2f}" if x < 1000 else f"₹{x:,.0f}"


def _pct(x: float, nd: int = 0) -> str:
    return f"{x * 100:+.{nd}f}%"


def _rel(x: float) -> str:
    """'12% above' / '7% below' / 'level with'."""
    return "level with" if abs(x) < 0.005 else f"{abs(x) * 100:.0f}% {'above' if x > 0 else 'below'}"


def _times(n: int) -> str:
    return "once" if n == 1 else "twice" if n == 2 else f"{n} times"


def _reading(s: dict) -> None:
    """Scores, a stance and a plain-words reading, all from fixed rules. Mutates s."""
    close = s["close"]; vs = s["vs"]; d = s["dma"]
    pts = 0
    above200 = vs.get("200") is not None and vs["200"] > 0
    rising200 = s.get("slope200") is not None and s["slope200"] > 0
    pts += 1 if above200 else 0
    pts += 1 if rising200 else 0
    pts += 1 if (vs.get("50") or -1) > 0 else 0
    pts += 1 if (d.get("50") and d.get("200") and d["50"] > d["200"]) else 0
    w = s.get("weekly") or {}
    pts += 1 if (w.get("above") and w.get("rising")) else 0
    has_trend = vs.get("200") is not None
    trend = max(1, pts) if has_trend else None
    pctl = s["arth"].get("pctl")
    mom = None if pctl is None else (5 if pctl >= 0.95 else 4 if pctl >= 0.80 else 3 if pctl >= 0.50 else 2 if pctl >= 0.20 else 1)
    tv = s.get("tv20") or 0
    liq = 5 if tv >= 50 else 4 if tv >= 10 else 3 if tv >= 2 else 2 if tv >= 0.5 else 1
    tight = s["series"] in ("BE", "BZ") or s.get("band") in ("2", "5")
    if tight:
        liq = max(1, liq - 1)
    atrp = s.get("atr_pct") or 0
    calm = 5 if atrp < 0.02 else 4 if atrp < 0.03 else 3 if atrp < 0.045 else 2 if atrp < 0.06 else 1
    s["scores"] = {"trend": trend, "momentum": mom, "liquidity": liq, "calm": calm}

    stretched = bool((vs.get("200") or 0) > 0.5 or (s.get("rsi") or 0) > 75 or (s.get("circ_up") or 0) >= 3)
    flags = []
    if stretched:
        flags.append("Stretched")
    if s["series"] in ("BE", "BZ"):
        flags.append("Trade-for-trade series")
    if s.get("band") in ("2", "5"):
        flags.append(f"{s['band']}% price band")
    if (s.get("locked") or 0) > 0:
        flags.append(f"{s['locked']} locked day{'s' if s['locked'] > 1 else ''} in 20")
    if tv < 2:
        flags.append("Thinly traded")
    if s["arth"].get("held"):
        flags.append("In the Arth paper book")
    s["flags"] = flags

    if trend is None:
        stance, cls = "Too new to read", "mixed"
    elif trend >= 5 and (mom or 0) >= 4:
        stance, cls = ("Strong uptrend, stretched: wait for a pullback" if stretched else "Strong uptrend"), "up"
    elif trend >= 4:
        stance, cls = ("Uptrend, stretched: wait for a pullback" if stretched else "Uptrend"), "up"
    elif above200 and not rising200:
        stance, cls = "Above a flat or falling long-term average: no clear trend", "mixed"
    elif trend >= 2 and above200:
        stance, cls = "Mixed: no clear trend", "mixed"
    elif not above200 and not rising200:
        stance, cls = "Downtrend: avoid until it builds a base", "down"
    else:
        stance, cls = "Below its long-term average: weak", "down"
    s["stance"], s["stance_class"] = stance, cls

    out = []
    if has_trend:
        out.append(f"Trend: the price is {_rel(vs['200'])} its 200-day average ({_inr(d['200'])}), which is "
                   f"{'rising' if rising200 else 'falling or flat'}"
                   + (f", and {_rel(vs['50'])} the 50-day ({_inr(d['50'])})." if vs.get("50") is not None else "."))
    else:
        out.append("Trend: fewer than 200 sessions of history, so there is no long-term average to judge it by yet.")
    a = s["arth"]
    if s.get("mom121") is not None:
        txt = f"Momentum: {_pct(s['mom121'])} from 12 months ago to 1 month ago"
        if a.get("rank"):
            txt += f"; on Arth's score it ranks {a['rank']:,} of {a['of']:,} liquid stocks"
            txt += " (the desk buys the top 15 and keeps a holding while it is in the top 30)." if a["rank"] <= 60 else "."
        elif a.get("why_out"):
            txt += f". It is outside Arth's universe: {a['why_out']}."
        else:
            txt += "."
        out.append(txt)
    elif a.get("why_out"):
        out.append(f"Momentum: not scored. It is outside Arth's universe: {a['why_out']}.")
    ex = s.get("exc") or {}
    if ex.get("6m") is not None:
        out.append(f"Against the market: {_pct(ex['6m'])} versus the Nifty 500 over 6 months"
                   + (f" and {_pct(ex['1m'])} over the last month." if ex.get("1m") is not None else "."))
    if stretched:
        why = []
        if (vs.get("200") or 0) > 0.5:
            why.append(f"{vs['200'] * 100:.0f}% above the 200-day average")
        if (s.get("rsi") or 0) > 75:
            why.append(f"RSI {s['rsi']:.0f}")
        if (s.get("circ_up") or 0) >= 3:
            why.append(f"{s['circ_up']} upper-band closes in 20 sessions")
        out.append("Stretched: " + ", ".join(why) + ". Sharp pullbacks are normal from here; chasing has poor odds.")
    lv = {}
    if s.get("atr"):
        lv["stop_atr"] = round(close - 2 * s["atr"], 2)
    if cls == "up" and d.get("20") and d.get("50"):
        lo, hi = sorted((d["20"], d["50"]))
        if hi < close:
            lv["entry_lo"], lv["entry_hi"] = round(lo, 2), round(hi, 2)
    # the level whose loss breaks the uptrend: the nearest swing low clearly under the pullback zone
    floor = (min(d["20"], d["50"]) if d.get("20") and d.get("50") else close) - 0.5 * (s.get("atr") or 0)
    clear = [x for x in s.get("sup", []) if x < floor]
    if cls == "up" and clear:
        lv["invalid"] = clear[0]
    s["levels"] = lv
    if cls == "up":
        txt = "Levels: "
        if "entry_lo" in lv:
            txt += f"the 20- and 50-day averages sit at {_inr(lv['entry_lo'])} to {_inr(lv['entry_hi'])}, the usual pullback zone. "
        if "invalid" in lv:
            txt += f"A weekly close below {_inr(lv['invalid'])} (a swing low) would break the uptrend. "
        if "stop_atr" in lv:
            txt += f"Two average daily ranges below the price is {_inr(lv['stop_atr'])} ({_pct(lv['stop_atr'] / close - 1)})."
        out.append(txt.strip())
    elif cls == "down" and d.get("50"):
        out.append(f"Repair signs to watch: a close back above the 50-day average at {_inr(d['50'])}"
                   + (f", then the 200-day at {_inr(d['200'])}." if d.get("200") else "."))
    elif s.get("sup") or s.get("res"):
        out.append("Levels: " + "; ".join(x for x in (
            f"support at {', '.join(_inr(v) for v in s['sup'][:2])}" if s.get("sup") else "",
            f"resistance at {', '.join(_inr(v) for v in s['res'][:2])}" if s.get("res") else "") if x) + ".")
    risk = f"Risk: it moves about {atrp * 100:.1f}% a day on average"
    if s.get("dd1y") is not None:
        risk += f"; its worst fall in the past year was {_pct(s['dd1y'])}"
    out.append(risk + ".")
    trade = f"Tradability: about ₹{tv:,.1f} crore changes hands a day"
    if s["series"] in ("BE", "BZ"):
        trade += "; it trades in the trade-for-trade series (delivery only, no intraday)"
    if s.get("band") in ("2", "5"):
        trade += f"; a {s['band']}% daily price band means you may not be able to sell on a bad day"
    if (s.get("circ_up") or 0) + (s.get("circ_dn") or 0) > 0:
        parts = [f"at the {nm} band {_times(k)}" for nm, k in (("upper", s.get("circ_up") or 0), ("lower", s.get("circ_dn") or 0)) if k]
        trade += "; in the last 20 sessions it closed " + " and ".join(parts)
    out.append(trade + ".")
    s["reading"] = out


def build(panel: Path, cm_dir: Path, out_dir: Path, db: Path | None = None, raw_dir: Path | None = None,
          now: dt.datetime | None = None) -> dict:
    P = pd.read_pickle(panel)
    C, RAW, V, TURN, META, IDX = P["C"], P["RAW"], P["V"], P["TURN"], P["META"], P["IDX"]
    t = C.index[-1]
    raw_dir = Path(raw_dir or ROOT / "data/raw")
    out_dir = Path(out_dir); adir = out_dir / "a"; sdir = adir / "s"
    if adir.exists():
        import shutil; shutil.rmtree(adir)
    sdir.mkdir(parents=True)

    live = RAW.loc[t].notna() & META.reindex(RAW.columns).last_series.isin(SERIES).values
    ents = list(RAW.columns[live.values])
    sym_now = {e: str(META.symbols[e]).split("|")[-1] for e in ents}
    n_hist = 300
    c = C[ents].iloc[-n_hist:]; raw = RAW[ents].iloc[-n_hist:]
    c = c.ffill(); v = V[ents].iloc[-n_hist:].fillna(0); turn = TURN[ents].iloc[-n_hist:]

    # adjusted highs and lows from the daily files: raw high/low times that day's adjusted/raw close ratio
    sym2ent = {}
    for e in ents:
        for sname in str(META.symbols[e]).split("|"):
            sym2ent[sname] = e
    rows = []
    for d in c.index:
        p = Path(cm_dir) / f"{d:%Y%m%d}.parquet"
        if p.exists():
            x = pd.read_parquet(p, columns=["symbol", "series", "high", "low", "close"])
            x = x[x.series.isin(SERIES)]
            x["entity"] = x.symbol.map(sym2ent); x["date"] = d
            rows.append(x.dropna(subset=["entity"]))
    ohl = pd.concat(rows).drop_duplicates(["date", "entity"], keep="last")
    hraw = ohl.pivot(index="date", columns="entity", values="high").reindex(index=c.index, columns=ents)
    lraw = ohl.pivot(index="date", columns="entity", values="low").reindex(index=c.index, columns=ents)
    ratio = (C[ents].iloc[-n_hist:] / raw).ffill()
    h = (hraw * ratio).fillna(c); l = (lraw * ratio).fillna(c)

    last = c.iloc[-1]
    def back(n):
        return (last / c.iloc[-1 - n] - 1) if len(c) > n else pd.Series(np.nan, index=ents)
    rets = {k: back(n) for k, n in (("1d", 1), ("1w", 5), ("1m", 21), ("3m", 63), ("6m", 126), ("12m", 252))}
    idx = IDX["Nifty_500"].reindex(c.index).ffill()
    exc = {k: rets[k] - (idx.iloc[-1] / idx.iloc[-1 - n] - 1) for k, n in (("1m", 21), ("3m", 63), ("6m", 126), ("12m", 252))
           if len(idx.dropna()) > n}
    dr = c.pct_change(fill_method=None)
    vol = dr.tail(252).std() * math.sqrt(252)
    vol[dr.tail(252).count() < 60] = np.nan
    mom121 = (c.iloc[-22] / c.iloc[-253] - 1) if len(c) > 253 else pd.Series(np.nan, index=ents)
    cfull = C[ents].ffill()                                   # averages use all the history there is
    ma = {n: cfull.rolling(n).mean().iloc[-n_hist:] for n in (20, 50, 100, 200)}
    slope200 = ma[200].iloc[-1] / ma[200].iloc[-21] - 1
    wk = c.resample("W-FRI").last()
    w30 = wk.rolling(30).mean()
    y = c.tail(252); yh = h.tail(252); yl = l.tail(252)
    hi52, lo52 = yh.max(), yl.min()
    hi52d, lo52d = yh.idxmax(), yl.idxmin()
    dd1y = (y / y.cummax() - 1).min()
    diff = c.diff()
    rsi = 100 - 100 / (1 + _wilder(diff.clip(lower=0), 14) / _wilder(-diff.clip(upper=0), 14))
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    sig = macd.ewm(span=9, adjust=False).mean()
    pc = c.shift()
    tr = np.maximum(h - l, np.maximum((h - pc).abs(), (l - pc).abs()))
    atr = _wilder(tr, 14)
    upm, dnm = h.diff(), -l.diff()
    pdi = 100 * _wilder(upm.where((upm > dnm) & (upm > 0), 0.0), 14) / atr
    ndi = 100 * _wilder(dnm.where((dnm > upm) & (dnm > 0), 0.0), 14) / atr
    adx = _wilder(100 * (pdi - ndi).abs() / (pdi + ndi), 14)
    tv20, tv120 = turn.tail(20).median() / 1e7, turn.tail(120).median() / 1e7
    vr = v.tail(20).mean() / v.tail(50).mean()
    up = dr.tail(50) > 0; dn = dr.tail(50) < 0
    updown = v.tail(50).where(up).sum() / v.tail(50).where(dn).sum().replace(0, np.nan)
    ir = idx.pct_change().tail(252)
    beta = dr.tail(252).apply(lambda col: col.cov(ir)) / ir.var()

    # Arth's universe and ranking on the same date, with the desk's own parameters
    params = {"top_n": 15, "universe_size": 1000, "min_price": 30.0, "min_history": 260, "min_tv": 2e7, "scorer": "121",
              "buffer_mult": 2.0}
    held = {}
    if db is not None and Path(db).exists():
        con = sqlite3.connect(db)
        try:
            meta = dict(con.execute("select key, value from meta"))
            params.update(json.loads(meta.get("params", "{}")))
            row = con.execute("select json from state order by date desc limit 1").fetchone()
            if row:
                held = json.loads(row[0]).get("pos", {})
        finally:
            con.close()
    f = S.Features.build(C, V, IDX["Nifty_500"], unadjusted=RAW)
    names = S.universe(f, t, int(params["universe_size"]), float(params["min_price"]), int(params["min_history"]),
                       float(params.get("min_tv", 0)))
    ranked = S.rank(f, t, names, str(params["scorer"]))
    order = list(ranked.index)
    rank_of = {e: i + 1 for i, e in enumerate(order)}
    score_all = (f.r231.loc[t] / f.vol.loc[t])
    uni_scores = score_all.reindex(names).dropna().sort_values()
    px_now = RAW.loc[t]; age = f.age.loc[t]; tvf = f.tv126.loc[t]
    floor_tv = float(tvf.reindex(names).min()) if names else 0.0

    sec = _sec_list(raw_dir / "sec_list.csv")
    band = dict(zip(sec.Symbol, sec.Band)); nm_sec = dict(zip(sec.Symbol, sec["Security Name"]))
    mc = _mcap(raw_dir)
    deliv = _delivery([d.date() for d in c.index[-10:]], raw_dir / "deliv")

    cal = [f"{d:%Y-%m-%d}" for d in c.index[-CHART_DAYS:]]
    index_rows, n_ok = [], 0
    seen = set()
    for e in ents:
        sym = sym_now[e]; key = _key(sym)
        if key in seen:
            continue
        seen.add(key)
        cl = float(last[e])
        if not math.isfinite(cl) or cl <= 0:
            continue
        series = str(META.last_series[e])
        b = band.get(sym)
        s = {"sym": sym, "key": key, "name": (mc["name"].get(sym) if sym in mc.index else None) or nm_sec.get(sym) or sym,
             "isin": str(META.isins[e]).split("|")[-1], "series": series, "band": b if b and b != "nan" else None,
             "date": f"{t:%Y-%m-%d}", "close": _r(float(raw[e].iloc[-1])), "chg": _r(rets["1d"][e], 4),
             "mcap": _r(mc["mcap"].get(sym) / 1e7, 0) if sym in mc.index and pd.notna(mc["mcap"].get(sym)) else None,
             "ret": {k: _r(rets[k][e], 4) for k in ("1w", "1m", "3m", "6m", "12m")},
             "exc": {k: _r(exc[k][e], 4) for k in exc},
             "mom121": _r(mom121[e], 4), "vol": _r(vol[e], 4),
             "dma": {str(n): _r(ma[n][e].iloc[-1]) for n in ma},
             "vs": {str(n): _r(cl / ma[n][e].iloc[-1] - 1, 4) for n in ma},
             "slope200": _r(slope200[e], 4),
             "hi52": _r(hi52[e]), "lo52": _r(lo52[e]),
             "hi52d": f"{hi52d[e]:%Y-%m-%d}" if pd.notna(hi52d[e]) else None,
             "lo52d": f"{lo52d[e]:%Y-%m-%d}" if pd.notna(lo52d[e]) else None,
             "from_hi": _r(cl / hi52[e] - 1, 4), "from_lo": _r(cl / lo52[e] - 1, 4), "dd1y": _r(dd1y[e], 4),
             "rsi": _r(rsi[e].iloc[-1], 1), "macd": _r(macd[e].iloc[-1]), "macd_sig": _r(sig[e].iloc[-1]),
             "macd_hist": _r((macd[e] - sig[e]).iloc[-1]), "adx": _r(adx[e].iloc[-1], 1),
             "pdi": _r(pdi[e].iloc[-1], 1), "ndi": _r(ndi[e].iloc[-1], 1),
             "atr": _r(atr[e].iloc[-1]), "atr_pct": _r(atr[e].iloc[-1] / cl, 4), "beta": _r(beta[e]),
             "tv20": _r(tv20[e]), "tv120": _r(tv120[e]), "vol_ratio": _r(vr[e]), "updown": _r(updown[e]),
             "deliv": _r(deliv.get(sym), 1) if len(deliv) else None}
        if len(wk) >= 31 and pd.notna(w30[e].iloc[-1]):
            s["weekly"] = {"above": bool(wk[e].iloc[-1] > w30[e].iloc[-1]), "rising": bool(w30[e].iloc[-1] > w30[e].iloc[-5]),
                           "vs": _r(wk[e].iloc[-1] / w30[e].iloc[-1] - 1, 4)}
        hh, ll = h[e].tail(250).dropna(), l[e].tail(250).dropna()
        s["sup"], s["res"] = _pivots(hh, ll, cl) if len(hh) > 15 else ([], [])
        if s["band"] and s["band"].replace(".", "").isdigit():
            bb = float(s["band"]) / 100
            ch20 = dr[e].tail(20); c20 = raw[e].tail(20); h20 = hraw[e].tail(20); l20 = lraw[e].tail(20)
            s["circ_up"] = int(((ch20 >= bb - 0.002) & (c20 >= h20 - 1e-6)).sum())
            s["circ_dn"] = int(((ch20 <= -bb + 0.002) & (c20 <= l20 + 1e-6)).sum())
            s["locked"] = int((h20 == l20).sum())
        a = {"held": int(held[sym]) if sym in held else 0}
        sc = score_all.get(e)
        if e in rank_of:
            a.update(rank=rank_of[e], of=len(order))
        else:
            why = []
            if not (px_now.get(e, 0) >= float(params["min_price"])):
                why.append(f"price under ₹{float(params['min_price']):.0f}")
            if not (age.get(e, 0) >= int(params["min_history"])):
                why.append(f"listed for fewer than {int(params['min_history'])} sessions")
            tvv = tvf.get(e)
            if pd.isna(tvv) or tvv < float(params.get("min_tv", 0)):
                why.append(f"trades under ₹{float(params.get('min_tv', 0)) / 1e7:.0f} crore a day")
            elif not why and tvv < floor_tv:
                why.append(f"not among the {int(params['universe_size']):,} most traded")
            if not why and (sc is None or pd.isna(sc)):
                why.append("not enough price history to score")
            a["why_out"] = " and ".join(why) or "not eligible"
        if sc is not None and pd.notna(sc) and len(uni_scores):
            a["score"] = _r(sc, 2)
            a["pctl"] = _r(float(np.searchsorted(uni_scores.values, sc, side="right")) / len(uni_scores), 3)
        s["arth"] = a
        _reading(s)
        tail = c[e].iloc[-CHART_DAYS:]
        s["px"] = [_r(x) for x in tail]
        s["ma50"] = [_r(x) for x in ma[50][e].iloc[-CHART_DAYS:]]
        s["ma200"] = [_r(x) for x in ma[200][e].iloc[-CHART_DAYS:]]
        (sdir / f"{key}.json").write_text(json.dumps(s, separators=(",", ":"), ensure_ascii=False))
        index_rows.append([sym, s["name"], key, 1 if a["held"] else 0, a.get("rank") or 0])
        n_ok += 1
    index_rows.sort(key=lambda r: r[0])
    now = now or dt.datetime.now(dt.timezone.utc)
    (adir / "index.json").write_text(json.dumps({"date": f"{t:%Y-%m-%d}", "built": now.astimezone(IST).strftime("%a %d %b %Y, %H:%M IST"),
                                                 "n": n_ok, "top_n": int(params["top_n"]),
                                                 "keep": int(round(float(params["buffer_mult"]) * int(params["top_n"]))),
                                                 "stocks": index_rows}, separators=(",", ":"), ensure_ascii=False))
    (adir / "cal.json").write_text(json.dumps(cal))
    (out_dir / "analyse.html").write_text(page(), encoding="utf-8")
    return {"stocks": n_ok, "date": f"{t:%Y-%m-%d}"}


# ---- the page -----------------------------------------------------------------------------------------------
PAGE_CSS = """
.search{position:relative}
.search input{width:100%;font:inherit;font-size:17px;padding:12px 14px;border-radius:12px;border:1px solid var(--rule);
background:var(--panel);color:var(--ink)}
.search input:focus-visible,.pick:focus-visible,.nav a:focus-visible{outline:2px solid var(--s1);outline-offset:2px}
.sugg{position:absolute;z-index:5;left:0;right:0;top:calc(100% + 4px);background:var(--panel);border:1px solid var(--rule);
border-radius:12px;max-height:320px;overflow:auto;box-shadow:0 8px 24px rgba(0,0,0,.14);list-style:none;margin:0;padding:4px}
.sugg li{padding:7px 10px;border-radius:8px;cursor:pointer;display:flex;gap:10px;align-items:baseline}
.sugg li[aria-selected=true],.sugg li:hover{background:var(--chip)}
.sugg b{font-weight:600;min-width:108px}.sugg span{color:var(--muted);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.picks{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:13px;color:var(--muted)}
.pick{font:inherit;font-size:12.5px;border:0;border-radius:999px;padding:2px 10px;background:var(--chip);color:var(--ink);cursor:pointer}
.head{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:baseline}
.head h2{font-size:22px;text-transform:none;letter-spacing:0;color:var(--ink);margin:0}
.head .sub{color:var(--muted);font-size:13px}
.stance{display:flex;flex-direction:column;gap:10px}
.badge{display:inline-flex;align-items:center;gap:8px;align-self:flex-start;border-radius:10px;padding:6px 12px;font-weight:600;font-size:15px}
.badge::before{content:"";width:9px;height:9px;border-radius:50%;background:currentColor}
.badge.up{background:var(--good-bg);color:var(--good)}.badge.mixed{background:var(--warn-bg);color:var(--warn)}
.badge.down{background:var(--bad-bg);color:var(--bad)}
.flags{display:flex;flex-wrap:wrap;gap:6px}
ul.read{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:7px;font-size:14.5px;line-height:1.5;max-width:90ch}
.scores{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.sc{display:flex;flex-direction:column;gap:4px}.sc .kl{font-size:12px;color:var(--muted)}
.dots{display:flex;gap:4px}.dots i{width:22px;height:6px;border-radius:3px;background:var(--rule)}.dots i.on{background:var(--ink)}
.sc .v{font-size:12.5px;color:var(--muted)}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12.5px;color:var(--muted);margin-bottom:6px}
.legend span::before{content:"";display:inline-block;width:16px;height:0;border-top:2px solid var(--c);vertical-align:middle;margin-right:6px}
.chart{position:relative}.chart svg{touch-action:pan-y}
.tip{position:absolute;pointer-events:none;background:var(--panel);border:1px solid var(--rule);border-radius:8px;padding:6px 9px;
font-size:12.5px;box-shadow:0 4px 14px rgba(0,0,0,.14);min-width:130px;display:none}
.tip .d{color:var(--muted);margin-bottom:3px}.tip .r{display:flex;justify-content:space-between;gap:12px}
.tip .r span::before{content:"";display:inline-block;width:10px;border-top:2px solid var(--c);vertical-align:middle;margin-right:5px}
.tip .r b{font-variant-numeric:tabular-nums;font-weight:600}
.s1{stroke:var(--s1)}.s2{stroke:var(--s2)}.s3{stroke:var(--s3)}
.ln{fill:none;stroke-width:2;stroke-linejoin:round}.ln.thin{stroke-width:1.5}
.cross{stroke:var(--muted);stroke-width:1}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,400px),1fr));gap:18px}
.kv.sm{font-size:19px;line-height:1.5}
.facts table td:first-child{color:var(--muted);white-space:normal}
.more a{color:var(--s1)}
"""

PAGE_JS = r"""
(function(){
var $=function(id){return document.getElementById(id)};
var IDX=null,CAL=null,cur=-1,shown=[];
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function inr(x){if(x==null)return '–';return '₹'+Number(x).toLocaleString('en-IN',{minimumFractionDigits:x<1000?2:0,maximumFractionDigits:x<1000?2:0})}
function pct(x,nd){if(x==null)return '–';var v=(x*100).toFixed(nd==null?1:nd);return (x>0?'+':'')+v+'%'}
function num(x,nd){return x==null?'–':Number(x).toFixed(nd==null?2:nd)}
function day(s){if(!s)return '–';var d=new Date(s+'T00:00:00');return d.toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric'})}
function cr(x){if(x==null)return '–';if(x>=1e5)return '₹'+(x/1e5).toFixed(2)+' lakh cr';return '₹'+Number(x).toLocaleString('en-IN',{maximumFractionDigits:x<10?1:0})+' cr'}
function cnt(x){return Number(x).toLocaleString('en-IN')}

fetch('a/index.json',{cache:'no-cache'}).then(function(r){return r.json()}).then(function(j){
  IDX=j;$('asof').textContent='Prices to '+day(j.date)+' · '+j.n.toLocaleString('en-IN')+' NSE shares · rebuilt '+j.built;
  var held=j.stocks.filter(function(s){return s[3]});
  if(held.length){var p=$('picks');p.appendChild(el('span',null,'In the paper book:'));
    held.forEach(function(s){var b=el('button','pick',s[0]);b.type='button';b.onclick=function(){go(s[0])};p.appendChild(b)})}
  return fetch('a/cal.json',{cache:'no-cache'}).then(function(r){return r.json()});
}).then(function(c){CAL=c;var h=decodeURIComponent(location.hash.slice(1));if(h)go(h)}).catch(function(){
  $('asof').textContent='The analyser data has not been built yet. It appears after the next nightly run.'});

var q=$('q'),sg=$('sugg');
function match(t){t=t.trim().toUpperCase();if(!t||!IDX)return [];
  var a=[],b=[],c=[];IDX.stocks.forEach(function(s){var sym=s[0],nm=s[1].toUpperCase();
    if(sym===t)a.unshift(s);else if(sym.indexOf(t)===0)a.push(s);else if(nm.indexOf(t)===0)b.push(s);else if(nm.indexOf(t)>0||sym.indexOf(t)>0)c.push(s)});
  return a.concat(b,c).slice(0,12)}
function draw(){shown=match(q.value);sg.textContent='';cur=-1;
  if(!shown.length){sg.hidden=true;q.setAttribute('aria-expanded','false');return}
  shown.forEach(function(s,i){var li=el('li');li.setAttribute('role','option');li.id='o'+i;
    li.appendChild(el('b',null,s[0]));li.appendChild(el('span',null,s[1]));
    li.onmousedown=function(e){e.preventDefault();go(s[0])};sg.appendChild(li)});
  sg.hidden=false;q.setAttribute('aria-expanded','true')}
function mark(){[].forEach.call(sg.children,function(li,i){li.setAttribute('aria-selected',i===cur?'true':'false');
  if(i===cur){li.scrollIntoView({block:'nearest'});q.setAttribute('aria-activedescendant',li.id)}})}
q.addEventListener('input',draw);
q.addEventListener('keydown',function(e){
  if(e.key==='ArrowDown'){e.preventDefault();if(shown.length){cur=(cur+1)%shown.length;mark()}}
  else if(e.key==='ArrowUp'){e.preventDefault();if(shown.length){cur=(cur-1+shown.length)%shown.length;mark()}}
  else if(e.key==='Enter'){e.preventDefault();var s=shown[cur<0?0:cur];if(s)go(s[0]);else miss()}
  else if(e.key==='Escape'){sg.hidden=true}});
q.addEventListener('blur',function(){setTimeout(function(){sg.hidden=true},120)});
window.addEventListener('hashchange',function(){var h=decodeURIComponent(location.hash.slice(1));if(h)go(h,true)});
function miss(){$('out').textContent='';$('out').appendChild(el('section','panel')).appendChild(el('div','empty'))
  .appendChild(el('strong',null,'No NSE share matches "'+q.value.trim()+'". Try the symbol (HFCL) or part of the company name.'))}

function go(sym,fromHash){if(!IDX)return;var row=IDX.stocks.filter(function(s){return s[0]===sym.toUpperCase()})[0];
  sg.hidden=true;if(!row){q.value=sym;miss();return}
  q.value=row[0];if(!fromHash)history.replaceState(null,'','#'+encodeURIComponent(row[0]));
  $('out').style.opacity=.5;
  fetch('a/s/'+row[2]+'.json',{cache:'no-cache'}).then(function(r){return r.json()}).then(render).catch(function(){
    $('out').style.opacity=1;$('out').textContent='Could not load '+row[0]+'. Try again in a minute.'})}

function kpi(l,v,s,c){var d=el('div','kpi');d.appendChild(el('div','kl',l));d.appendChild(el('div','kv '+(c||''),v));d.appendChild(el('div','ks',s||''));return d}
function tr(tb,k,v){var r=el('tr');r.appendChild(el('td',null,k));r.appendChild(el('td','n',v));tb.appendChild(r)}
function table(title,rows){var s=el('section','panel');s.appendChild(el('h2',null,title));var w=el('div','scroll'),t=el('table'),b=el('tbody');
  rows.forEach(function(x){tr(b,x[0],x[1])});t.appendChild(b);w.appendChild(t);s.appendChild(w);return s}
function score(l,n,txt){var d=el('div','sc');d.appendChild(el('div','kl',l));var dots=el('div','dots');dots.setAttribute('role','img');
  dots.setAttribute('aria-label',n==null?'not scored':n+' out of 5');
  for(var i=1;i<=5;i++){dots.appendChild(el('i',n!=null&&i<=n?'on':''))}d.appendChild(dots);d.appendChild(el('div','v',(n==null?'n/a':n+'/5')+' · '+txt));return d}

function render(s){var o=$('out');o.textContent='';o.style.opacity=1;
  var hd=el('section','panel stance');var h=el('div','head');h.appendChild(el('h2',null,s.name));
  h.appendChild(el('span','sub',s.sym+' · '+s.series+(s.band?' · '+(isNaN(s.band)?s.band:s.band+'% band'):'')+' · ISIN '+s.isin));hd.appendChild(h);
  hd.appendChild(el('span','badge '+s.stance_class,s.stance));
  if(s.flags.length){var fl=el('div','flags');s.flags.forEach(function(f){fl.appendChild(el('span','chip',f))});hd.appendChild(fl)}
  var ul=el('ul','read');s.reading.forEach(function(t){ul.appendChild(el('li',null,t))});hd.appendChild(ul);
  var sc=el('div','scores');var S=s.scores;
  sc.appendChild(score('Trend',S.trend,'price against its averages'));
  sc.appendChild(score('Momentum',S.momentum,s.arth.rank?('Arth rank '+cnt(s.arth.rank)+' of '+cnt(s.arth.of)):'against liquid stocks'));
  sc.appendChild(score('Ease of trading',S.liquidity,cr(s.tv20)+' a day'));
  sc.appendChild(score('Calmness',S.calm,'moves '+pct(s.atr_pct).replace('+','')+' a day'));hd.appendChild(sc);
  o.appendChild(hd);

  var k=el('section','kpis');
  k.appendChild(kpi('Close · '+day(s.date),inr(s.close),pct(s.chg)+' on the day',s.chg>0?'pos':s.chg<0?'neg':''));
  k.appendChild(kpi('1 month',pct(s.ret['1m']),'vs Nifty 500 '+pct(s.exc['1m']),s.ret['1m']>0?'pos':'neg'));
  k.appendChild(kpi('6 months',pct(s.ret['6m'],0),'vs Nifty 500 '+pct(s.exc['6m'],0),s.ret['6m']>0?'pos':'neg'));
  k.appendChild(kpi('12 months',pct(s.ret['12m'],0),'vs Nifty 500 '+pct(s.exc['12m'],0),s.ret['12m']>0?'pos':'neg'));
  k.appendChild(kpi('From 52-week high',pct(s.from_hi),inr(s.hi52)+' on '+day(s.hi52d)));
  k.appendChild(kpi('Market value',s.mcap==null?'–':cr(s.mcap),'NSE, full market cap','sm'));
  o.appendChild(k);

  var cp=el('section','panel');cp.appendChild(el('h2',null,'Price, last '+s.px.length+' sessions (adjusted for splits and bonuses)'));
  var lg=el('div','legend');[['Close','--s1'],['50-day average','--s2'],['200-day average','--s3']].forEach(function(x){
    var sp=el('span',null,x[0]);sp.style.setProperty('--c','var('+x[1]+')');lg.appendChild(sp)});cp.appendChild(lg);
  var box=el('div','chart');cp.appendChild(box);o.appendChild(cp);chart(box,s);

  var f=el('div','facts');
  f.appendChild(table('Trend',[['20-day average',inr(s.dma['20'])+' ('+pct(s.vs['20'])+')'],['50-day average',inr(s.dma['50'])+' ('+pct(s.vs['50'])+')'],
    ['100-day average',inr(s.dma['100'])+' ('+pct(s.vs['100'])+')'],['200-day average',inr(s.dma['200'])+' ('+pct(s.vs['200'])+')'],
    ['200-day average, 1-month change',pct(s.slope200)],['30-week average',s.weekly?((s.weekly.above?'above':'below')+', '+(s.weekly.rising?'rising':'falling')):'–'],
    ['52-week low',inr(s.lo52)+' on '+day(s.lo52d)],['Worst fall, past year',pct(s.dd1y)]]));
  f.appendChild(table('Momentum',[['1 week',pct(s.ret['1w'])],['3 months',pct(s.ret['3m'])],['12 months to 1 month ago',pct(s.mom121,0)],
    ['Arth score (that return ÷ volatility)',num(s.arth.score)],['Arth rank',s.arth.rank?(cnt(s.arth.rank)+' of '+cnt(s.arth.of)):('outside: '+(s.arth.why_out||'–'))],
    ['RSI (14)',num(s.rsi,1)],['MACD / signal',num(s.macd)+' / '+num(s.macd_sig)],['ADX (14), +DI / −DI',num(s.adx,1)+', '+num(s.pdi,1)+' / '+num(s.ndi,1)]]));
  f.appendChild(table('Levels',[['Resistance (swing highs)',s.res.length?s.res.map(inr).join(', '):'none above: at its highs'],
    ['Support (swing lows)',s.sup.length?s.sup.map(inr).join(', '):'none below: at its lows'],
    ['Pullback zone (20–50 day)',s.levels.entry_lo?inr(s.levels.entry_lo)+' – '+inr(s.levels.entry_hi):'–'],
    ['Uptrend breaks below',s.levels.invalid?inr(s.levels.invalid)+' (weekly close)':'–'],
    ['Two daily ranges below price',s.levels.stop_atr?inr(s.levels.stop_atr):'–']]));
  f.appendChild(table('Risk and tradability',[['Average daily range (ATR 14)',inr(s.atr)+' ('+pct(s.atr_pct).replace('+','')+')'],
    ['Volatility, 1 year',pct(s.vol,0).replace('+','')],['Beta to Nifty 500',num(s.beta)],
    ['Traded a day (20 / 120 sessions)',cr(s.tv20)+' / '+cr(s.tv120)],['Volume, 20-day vs 50-day',num(s.vol_ratio)+'×'],
    ['Up-day vs down-day volume (50)',num(s.updown)+'×'],['Delivered, 10 sessions',s.deliv==null?(s.series==='EQ'?'–':'all trades (trade-for-trade)'):num(s.deliv,0)+'%'],
    ['Upper / lower band closes, 20 sessions',s.circ_up==null?'no fixed band':(s.circ_up+' / '+s.circ_dn+(s.locked?', '+s.locked+' locked':''))],
    ['Paper book',s.arth.held?(cnt(s.arth.held)+' shares held'):'not held']]));
  o.appendChild(f);

  var m=el('p','note more');m.appendChild(document.createTextNode('This page reads price and volume only. For the business, results, valuation, ownership and governance, see '));
  var a=el('a',null,'Screener');a.href='https://www.screener.in/company/'+encodeURIComponent(s.sym)+'/consolidated/';a.target='_blank';a.rel='noopener';m.appendChild(a);
  m.appendChild(document.createTextNode(' or '));
  var b=el('a',null,'NSE');b.href='https://www.nseindia.com/get-quotes/equity?symbol='+encodeURIComponent(s.sym);b.target='_blank';b.rel='noopener';m.appendChild(b);
  m.appendChild(document.createTextNode(', or ask Claude to "analyse '+s.sym+'" for the full written report.'));o.appendChild(m);
}

function chart(box,s){var W=760,H=280,pl=8,pr=64,pt=10,pb=24,n=s.px.length;
  var all=s.px.concat(s.ma50,s.ma200).filter(function(v){return v!=null});if(!all.length)return;
  var lo=Math.min.apply(null,all),hi=Math.max.apply(null,all),pad=(hi-lo)*0.06||1;lo-=pad;hi+=pad;
  function X(i){return pl+(W-pl-pr)*(n>1?i/(n-1):0)}function Y(v){return pt+(H-pt-pb)*(1-(v-lo)/(hi-lo))}
  var ns='http://www.w3.org/2000/svg';function S(t,a){var e=document.createElementNS(ns,t);for(var k in a)e.setAttribute(k,a[k]);return e}
  var svg=S('svg',{viewBox:'0 0 '+W+' '+H,role:'img','aria-label':'Closing price of '+s.sym+' with its 50-day and 200-day averages'});
  var step=Math.pow(10,Math.floor(Math.log10((hi-lo)/4))),raw=(hi-lo)/4/step;step*=raw<1.5?1:raw<3.5?2:raw<7.5?5:10;
  for(var g=Math.ceil(lo/step)*step;g<hi;g+=step){svg.appendChild(S('line',{x1:pl,x2:W-pr,y1:Y(g),y2:Y(g),'class':'grid'}));
    var t=S('text',{x:W-pr+6,y:Y(g)+4,'class':'tick'});t.textContent=Number(g.toFixed(2)).toLocaleString('en-IN');svg.appendChild(t)}
  var off=CAL.length-n;[0,Math.floor(n/2),n-1].forEach(function(i,j){var t=S('text',{x:X(i),y:H-6,'class':'tick','text-anchor':j===0?'start':j===1?'middle':'end'});
    t.textContent=day(CAL[off+i]);svg.appendChild(t)});
  function path(arr){var d='',pen=false;arr.forEach(function(v,i){if(v==null){pen=false;return}d+=(pen?'L':'M')+X(i).toFixed(1)+' '+Y(v).toFixed(1);pen=true});return d}
  svg.appendChild(S('path',{d:path(s.ma200),'class':'ln thin s3'}));svg.appendChild(S('path',{d:path(s.ma50),'class':'ln thin s2'}));
  svg.appendChild(S('path',{d:path(s.px),'class':'ln s1'}));
  var cross=S('line',{y1:pt,y2:H-pb,'class':'cross',visibility:'hidden'});svg.appendChild(cross);
  var hit=S('rect',{x:pl,y:pt,width:W-pl-pr,height:H-pt-pb,fill:'transparent',tabindex:'0','aria-label':'Chart: use left and right arrow keys to read values'});svg.appendChild(hit);
  box.appendChild(svg);var tip=el('div','tip');box.appendChild(tip);var at=n-1;
  function show(i){at=Math.max(0,Math.min(n-1,i));cross.setAttribute('x1',X(at));cross.setAttribute('x2',X(at));cross.setAttribute('visibility','visible');
    tip.textContent='';tip.appendChild(el('div','d',day(CAL[off+at])));
    [['Close',s.px[at],'--s1'],['50-day',s.ma50[at],'--s2'],['200-day',s.ma200[at],'--s3']].forEach(function(r){
      var row=el('div','r');var sp=el('span',null,r[0]);sp.style.setProperty('--c','var('+r[2]+')');row.appendChild(sp);row.appendChild(el('b',null,inr(r[1])));tip.appendChild(row)});
    tip.style.display='block';var bw=box.clientWidth,x=X(at)/W*bw,tw=tip.offsetWidth;tip.style.left=Math.max(0,Math.min(bw-tw,x+(x>bw/2?-tw-10:10)))+'px';tip.style.top='8px'}
  function hide(){tip.style.display='none';cross.setAttribute('visibility','hidden')}
  hit.addEventListener('pointermove',function(e){var r=svg.getBoundingClientRect();var x=(e.clientX-r.left)/r.width*W;show(Math.round((x-pl)/(W-pl-pr)*(n-1)))});
  hit.addEventListener('pointerleave',hide);hit.addEventListener('blur',hide);hit.addEventListener('focus',function(){show(at)});
  hit.addEventListener('keydown',function(e){if(e.key==='ArrowLeft'){e.preventDefault();show(at-1)}else if(e.key==='ArrowRight'){e.preventDefault();show(at+1)}});
}
})();
"""


def page() -> str:
    from arth.ops import report
    css = report.CSS + PAGE_CSS
    series = (":root{--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a}"
              "@media (prefers-color-scheme:dark){:root:not([data-theme=\"light\"]){--s1:#3987e5;--s2:#d95926;--s3:#199e70}}"
              ":root[data-theme=\"dark\"]{--s1:#3987e5;--s2:#d95926;--s3:#199e70}")
    body = """<div class="wrap">
<header class="top"><h1>Arth <span>stock analyser</span></h1>
<nav class="nav" aria-label="Sections"><a href="./">Paper desk</a><a href="analyse.html" aria-current="page">Analyse a stock</a></nav></header>
<div class="search"><input id="q" type="search" placeholder="Type an NSE symbol or company name, e.g. HFCL or Laurus" autocomplete="off"
 autocapitalize="characters" spellcheck="false" role="combobox" aria-expanded="false" aria-controls="sugg" aria-autocomplete="list"
 aria-label="Search for an NSE share">
<ul id="sugg" class="sugg" role="listbox" hidden></ul></div>
<div id="picks" class="picks"></div>
<div id="out" style="display:flex;flex-direction:column;gap:18px;transition:opacity .15s"></div>
<p class="note"><span id="asof">Loading…</span><br>A mechanical reading of NSE prices and volumes, rebuilt after each trading day
with the paper desk. Fixed rules produce the words and scores; nothing here knows about a company's business, results,
valuation or management. It is not investment advice.</p>
</div>"""
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Arth stock analyser</title>\n{report.FONTS}\n<style>{css}{series}</style></head><body>{body}"
            f"<script>{PAGE_JS}</script></body></html>")
