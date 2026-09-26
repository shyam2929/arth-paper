"""
Research engine v2 for the new build (codename "Arth").
- Universe: ALL currently-listed NSE stocks with Upstox history (not today's index members), filtered
  point-in-time by liquidity -> removes index-inclusion look-ahead; residual bias = names delisted before 2026.
- Signals use data up to t-1 close; trades execute at t close with slippage; Upstox delivery fees per order.
- Idle cash earns CASH_YIELD (assumes sweep to a liquid ETF); borrowed money (MTF) pays MTF_RATE.
"""
import math, sys, numpy as np, pandas as pd
from arth.ledger.fees import charges as _charges, LEGACY as _LEGACY

import os
P = pd.read_pickle(os.environ.get('ARTH_PANEL', str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'data/panel_nse.pkl')))
C_raw, V = P['C'], P['V']
IDX = P['IDX']
D = C_raw.index
C = C_raw.ffill()
STOCKS = [c for c in C.columns if not c.startswith('ETF_')]
CASH_YIELD = 0.05
TD = 252

def fee(side, value):
    return _charges(side, value, _LEGACY) if value > 0 else 0.0

# ---------------- precomputed features (all as of close of the row date) ----------------
Cs = C[STOCKS]
ret1 = Cs.pct_change(fill_method=None)
vol252 = ret1.rolling(252, min_periods=200).std() * math.sqrt(TD)
r126 = Cs / Cs.shift(126) - 1
r252 = Cs / Cs.shift(252) - 1
r231 = Cs.shift(21) / Cs.shift(252) - 1          # 12-1 month
tv126 = (C_raw[STOCKS] * V[STOCKS]).rolling(126, min_periods=100).median()
age = C_raw[STOCKS].notna().cumsum()
n500 = IDX['Nifty_500'].reindex(D).ffill()
n500_dma200 = n500.rolling(200).mean()
gold = C['ETF_GOLDBEES'] if 'ETF_GOLDBEES' in C.columns else None

PXF = P['RAW'][STOCKS].ffill() if 'RAW' in P else Cs      # unadjusted price for the minimum-price rule

def universe(t, top=500, min_px=30, min_tv=0):
    tv = tv126.loc[t]
    ok = (PXF.loc[t] >= min_px) & (age.loc[t] >= 260) & tv.notna() & vol252.loc[t].notna() & r252.loc[t].notna()
    if min_tv: ok &= tv >= min_tv
    names = tv[ok].sort_values(ascending=False).index[:top]
    return list(names)

def score_nms(t, names):
    z6 = r126.loc[t, names] / vol252.loc[t, names]
    z12 = r252.loc[t, names] / vol252.loc[t, names]
    s = 0.5 * ((z6 - z6.mean()) / z6.std()) + 0.5 * ((z12 - z12.mean()) / z12.std())
    return s.sort_values(ascending=False)

def score_121(t, names):
    return (r231.loc[t, names] / vol252.loc[t, names]).sort_values(ascending=False)

def rebal_dates(freq, start, end):
    ds = D[(D >= pd.Timestamp(start)) & (D <= pd.Timestamp(end))]
    s = pd.Series(ds, index=ds)
    if freq == 'M':  key = s.dt.to_period('M')
    elif freq == 'W': key = s.dt.to_period('W')
    elif freq == 'Q': key = s.dt.to_period('Q')
    elif freq == 'H': key = s.dt.year.astype(str) + '-' + (s.dt.month > 6).astype(str)
    return set(s.groupby(key).first().tolist())

def run(start='2018-02-01', end='2026-09-18', cap=1_000_000, N=20, buffer=2.0, freq='M', top=500,
        scorer='nms', overlay=None, lev=1.0, vol_target=None, mtf_rate=0.1825, slip=0.0015,
        band=0.25, min_tv=0, fees=True, gold_switch=False, weight='equal', label=''):
    ff = (lambda s, v: fee(s, v)) if fees else (lambda s, v: 0.0)
    days = D[(D >= pd.Timestamp(start)) & (D <= pd.Timestamp(end))]
    rb = rebal_dates(freq, start, end)
    cash = float(cap); pos = {}; eq = []; fees_paid = 0.0; turnover = 0.0; ntr = 0; mcalls = 0
    held_since = {}; hold_days = []; entry_px = {}; tlog = []
    prev = None
    for t in days:
        px = C.loc[t]
        # accrue carry on cash (+) or MTF interest (-)
        if prev is not None:
            dt_days = (t - prev).days
            cash += cash * (CASH_YIELD if cash > 0 else mtf_rate) * dt_days / 365.0
        mv = sum(q * px[s] for s, q in pos.items())
        equity = cash + mv
        # margin call: equity below 25% of gross exposure -> cut to 1x
        if mv > 0 and equity < 0.25 * mv:
            mcalls += 1
            scale = max(0.0, equity) / mv
            for s in list(pos):
                sell_q = pos[s] - math.floor(pos[s] * scale)
                if sell_q > 0:
                    v = sell_q * px[s] * (1 - slip); f = ff('SELL', v); cash += v - f; fees_paid += f; turnover += v; ntr += 1
                    pos[s] -= sell_q
                    if pos[s] == 0: del pos[s]
        if t in rb:
            i = D.get_loc(t); s_ = D[i - 1]           # signal date = previous close
            names = universe(s_, top=top, min_tv=min_tv)
            ranked = {'nms': score_nms, '121': score_121, 'ens': score_ens}[scorer](s_, names)
            rank = pd.Series(np.arange(1, len(ranked) + 1), index=ranked.index)
            # exposure
            expo = lev
            risk_on = True
            if overlay == 'trend':
                risk_on = n500.loc[s_] > n500_dma200.loc[s_]
                if not risk_on: expo = lev * 0.5
            if overlay == 'trend_off':
                risk_on = n500.loc[s_] > n500_dma200.loc[s_]
                if not risk_on: expo = 0.0
            if vol_target:
                # realised vol of the current strategy equity over ~63d
                if len(eq) > 64:
                    e = pd.Series([x[1] for x in eq[-64:]])
                    rv = e.pct_change().std() * math.sqrt(TD)
                    if rv > 0: expo = min(lev, expo * vol_target / rv) if overlay else min(lev, vol_target / rv)
            keep = [s for s in pos if s in rank.index and rank[s] <= buffer * N and not s.startswith('ETF_')]
            keep = sorted(keep, key=lambda s: rank[s])[:N]
            target = list(keep) + [s for s in ranked.index if s not in keep][: N - len(keep)]
            mv = sum(q * px[s] for s, q in pos.items()); equity = cash + mv
            per = equity * expo / N if N else 0
            if weight == 'invvol':
                iv = (1 / vol252.loc[s_, target]).fillna(0); iv = iv / iv.sum()
                tw = {s: equity * expo * iv[s] for s in target}
            else:
                tw = {s: per for s in target}
            # gold sleeve when risk-off (dual momentum)
            gold_w = 0.0
            if gold_switch and not risk_on:
                g6 = gold.loc[s_] / gold.shift(126).loc[s_] - 1
                if g6 > 0: gold_w = equity * (lev - expo)
            if gold_w > 0: tw['ETF_GOLDBEES'] = gold_w
            # sells first
            for s in list(pos):
                p = px[s]
                tgt_q = math.floor(tw.get(s, 0) / p) if s in tw else 0
                diff = pos[s] - tgt_q
                if s not in tw or (diff > 0 and diff * p > band * tw[s]):
                    v = diff * p * (1 - slip); f = ff('SELL', v); cash += v - f; fees_paid += f; turnover += v; ntr += 1
                    pos[s] = tgt_q
                    if pos[s] <= 0:
                        del pos[s]
                        if s in held_since:
                            hold_days.append((t - held_since[s]).days)
                            tlog.append((s, held_since.pop(s), t, entry_px.pop(s, p), p))
            for s, w in tw.items():
                p = px[s]
                if not (p > 0): continue
                tgt_q = math.floor(w / p); cur = pos.get(s, 0); diff = tgt_q - cur
                if diff > 0 and (cur == 0 or diff * p > band * w):
                    v = diff * p * (1 + slip); f = ff('BUY', v)
                    cash -= v + f; fees_paid += f; turnover += v; ntr += 1
                    pos[s] = cur + diff
                    held_since.setdefault(s, t); entry_px.setdefault(s, p)
        mv = sum(q * px[s] for s, q in pos.items())
        eq.append((t, cash + mv, mv))
        prev = t
    e = pd.Series([x[1] for x in eq], index=[x[0] for x in eq])
    gross = pd.Series([x[2] for x in eq], index=e.index)
    return dict(equity=e, gross=gross, fees=fees_paid, turnover=turnover, trades=ntr, margin_calls=mcalls,
                hold_days=hold_days, label=label, cap=cap, tlog=tlog, open_pos={s_: (held_since.get(s_), entry_px.get(s_), q) for s_, q in pos.items()})

def stats(res_or_eq, rf=0.06):
    if isinstance(res_or_eq, dict): e = res_or_eq['equity']; r = res_or_eq
    else: e = res_or_eq.dropna(); r = None
    yrs = (e.index[-1] - e.index[0]).days / 365.25
    cagr = (e.iloc[-1] / e.iloc[0]) ** (1 / yrs) - 1
    d = e.pct_change().dropna(); vol = d.std() * math.sqrt(TD)
    dd = (e / e.cummax() - 1).min()
    m = e.resample('ME').last().pct_change().dropna()
    out = dict(CAGR=round(cagr * 100, 1), Vol=round(vol * 100, 1), MaxDD=round(dd * 100, 1),
               Sharpe=round((d.mean() * TD - rf) / vol, 2) if vol else None,
               Calmar=round(cagr / abs(dd), 2) if dd < 0 else None,
               mo_mean=round(m.mean() * 100, 2), mo_median=round(m.median() * 100, 2),
               mo_ge6=round((m >= 0.06).mean() * 100, 1), mo_neg=round((m < 0).mean() * 100, 1),
               mo_worst=round(m.min() * 100, 1), mo_best=round(m.max() * 100, 1))
    if r:
        avg_eq = e.mean()
        out.update(fee_pct=round(r['fees'] / avg_eq / yrs * 100, 2), turn=round(r['turnover'] / 2 / avg_eq / yrs, 1),
                   trades=r['trades'], mcalls=r['margin_calls'],
                   hold_med_d=int(np.median(r['hold_days'])) if r['hold_days'] else None)
    return out

def score_ens(t, names):
    a = score_nms(t, names).rank(ascending=False)
    b = score_121(t, names).rank(ascending=False)
    return (a + b).sort_values()   # lower combined rank = better
