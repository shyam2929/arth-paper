"""Daily report: a self-contained HTML dashboard and a short text summary.

`write(db, panel)` writes reports/index.html as a full document (the server serves it on localhost).
`write(db, panel, out_dir=..., fragment=True)` writes the same page without <html>/<head>/<body> wrappers,
which is the form the Claude artifact host expects (it adds its own skeleton).
"""
from __future__ import annotations
import datetime as dt, html, json, math, sqlite3
from pathlib import Path
import pandas as pd

from arth.ledger.lots import Realised, tax_by_year

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "reports"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _load(db: Path):
    con = sqlite3.connect(db)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    days = pd.read_sql("SELECT * FROM days ORDER BY date", con, parse_dates=["date"]).set_index("date")
    fills = pd.read_sql("SELECT * FROM fills ORDER BY id", con)
    st = con.execute("SELECT date, json FROM state ORDER BY date DESC LIMIT 1").fetchone()
    state = json.loads(st[1]) if st else {}
    plans = pd.read_sql("SELECT * FROM plans ORDER BY signal_date DESC, rank", con)
    events = pd.read_sql("SELECT * FROM events ORDER BY ts DESC, rowid DESC LIMIT 40", con)
    return meta, days, fills, state, plans, events


def _pct(x, d=1):
    return "–" if x is None or pd.isna(x) else f"{x * 100:+.{d}f}%"


def _group_in(n: int) -> str:
    """Indian digit grouping: 1234567 -> 12,34,567."""
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:]); head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def _inr(x):
    if x is None or pd.isna(x):
        return "–"
    return ("-" if x < 0 else "") + "₹" + _group_in(int(round(abs(x))))


def _px(x):
    return "–" if x is None or pd.isna(x) else f"{x:,.2f}"


def _d(s, fmt="%a %d %b %Y"):
    try:
        return pd.Timestamp(s).strftime(fmt)
    except Exception:
        return str(s)


def CAL_prev(day) -> str | None:
    """The trading session before `day` (NSE calendar), as ISO text."""
    if not day:
        return None
    from arth.data import calendar as CAL
    d = dt.date.fromisoformat(str(day)) - dt.timedelta(days=1)
    while not CAL.is_session(d):
        d -= dt.timedelta(days=1)
    return d.isoformat()


def next_month_start(day) -> str | None:
    """The first trading session of the month after `day`."""
    if day is None:
        return None
    from arth.data import calendar as CAL
    d = pd.Timestamp(day).date()
    while True:
        n = CAL.next_session(d)
        if n.month != pd.Timestamp(day).month:
            return n.isoformat()
        d = n


def summary(db: Path) -> str:
    meta, days, fills, state, plans, events = _load(db)
    if days.empty:
        txt = f"Arth paper: waiting for the first session ({_d(meta.get('start'))})."
        if len(plans):
            txt += f" Target list ready: {len(plans[plans.signal_date == plans.signal_date.iloc[0]])} names."
        return txt
    cap = float(meta["capital"]); last = days.iloc[-1]
    peak = days.equity.cummax().iloc[-1]
    txt = (f"Arth paper {days.index[-1]:%d %b %Y}: equity {_inr(last.equity)} ({_pct(last.equity / cap - 1)} since "
           f"{_d(meta['start'], '%d %b')}), momentum index {_pct(last.fund / cap - 1)}, drawdown {_pct(last.equity / peak - 1)}, "
           f"{int(last.positions)} holdings.")
    today_fills = fills[fills.date == days.index[-1].date().isoformat()]
    if len(today_fills):
        txt += f" Rebalanced: {len(today_fills)} orders."
    if len(plans) and str(plans.signal_date.iloc[0]) == days.index[-1].date().isoformat():
        txt += f" Next session is a rebalance; {len(plans[plans.signal_date == plans.signal_date.iloc[0]])} names on the target list."
    return txt


def _svg_chart(days: pd.DataFrame, cap: float) -> str:
    if len(days) < 2:
        return ("<div class='empty'><strong>The equity curve starts after the second session.</strong>"
                "<span>Arth (solid) against the same money in the Midcap150 Momentum 50 index (dashed).</span></div>")
    w, h, padl, padr, padt, padb = 760, 250, 44, 86, 14, 28
    a = days.equity / cap; b = days.fund / cap
    lo, hi = min(a.min(), b.min(), 1.0), max(a.max(), b.max(), 1.0)
    span = hi - lo or 0.02
    lo, hi = lo - span * 0.08, hi + span * 0.08
    n = len(days)
    X = lambda i: padl + (w - padl - padr) * i / (n - 1)
    Y = lambda v: padt + (h - padt - padb) * (hi - v) / (hi - lo)
    pa = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(a))
    pb = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(b))
    area = f"{X(0):.1f},{Y(lo):.1f} " + pa + f" {X(n - 1):.1f},{Y(lo):.1f}"
    rng = (hi - lo) * 100                                  # percent span; ticks on round numbers
    step = next(x for x in (0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200) if rng / x <= 5)
    k0, k1 = math.ceil((lo - 1) * 100 / step), math.floor((hi - 1) * 100 / step)
    tv = [1 + k * step / 100 for k in range(k0, k1 + 1)]
    ticks = "".join(f"<line x1='{padl}' x2='{w - padr}' y1='{Y(v):.1f}' y2='{Y(v):.1f}' class='{'base' if abs(v - 1) < 1e-9 else 'grid'}'/>"
                    f"<text x='{padl - 6}' y='{Y(v) + 4:.1f}' class='tick' text-anchor='end'>{'0' if abs(v - 1) < 1e-9 else f'{(v - 1) * 100:+g}'}%</text>"
                    for v in tv)
    ya, yb = Y(a.iloc[-1]), Y(b.iloc[-1])
    if abs(ya - yb) < 16:
        ya, yb = (ya - 8, yb + 8) if ya <= yb else (ya + 8, yb - 8)
    return (f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='Arth equity against the momentum index fund'>{ticks}"
            f"<polygon points='{area}' class='area'/>"
            f"<polyline points='{pb}' class='fund'/><polyline points='{pa}' class='arth'/>"
            f"<circle cx='{X(n - 1):.1f}' cy='{Y(a.iloc[-1]):.1f}' r='3.5' class='dot'/>"
            f"<text x='{w - padr + 8}' y='{ya + 4:.1f}' class='lab arthl'>Arth {_pct(a.iloc[-1] - 1)}</text>"
            f"<text x='{w - padr + 8}' y='{yb + 4:.1f}' class='lab fundl'>Index {_pct(b.iloc[-1] - 1)}</text>"
            f"<text x='{padl}' y='{h - 8}' class='tick'>{days.index[0]:%d %b %Y}</text>"
            f"<text x='{w - padr}' y='{h - 8}' class='tick' text-anchor='end'>{days.index[-1]:%d %b %Y}</text></svg>")


CSS = """
:root{--ground:#f3f5f2;--panel:#ffffff;--ink:#16201c;--muted:#5a655f;--rule:#dbe1dc;--chip:#e7ece8;
--arth:#0e5a6b;--arth-soft:rgba(14,90,107,.10);--fund:#a2660f;--good:#1b7a45;--bad:#b42318;--warn:#8f5f00;--warn-bg:#fbefd2;
--good-bg:#e3f2e8;--bad-bg:#fbe4e1;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--ground:#0f1413;--panel:#161c1a;--ink:#e3e9e5;--muted:#95a29b;
--rule:#27312d;--chip:#1f2926;--arth:#62bccd;--arth-soft:rgba(98,188,205,.12);--fund:#e2aa4c;--good:#5fcc8c;--bad:#f0857a;
--warn:#e6b84a;--warn-bg:#33290f;--good-bg:#15301f;--bad-bg:#3a1a17;color-scheme:dark}}
:root[data-theme="dark"]{--ground:#0f1413;--panel:#161c1a;--ink:#e3e9e5;--muted:#95a29b;--rule:#27312d;--chip:#1f2926;
--arth:#62bccd;--arth-soft:rgba(98,188,205,.12);--fund:#e2aa4c;--good:#5fcc8c;--bad:#f0857a;--warn:#e6b84a;--warn-bg:#33290f;
--good-bg:#15301f;--bad-bg:#3a1a17;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font:15px/1.5 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding-inline:16px;padding-block:22px 56px;display:flex;flex-direction:column;gap:18px}
h1,h2,.kv{font-family:"IBM Plex Sans Condensed","Arial Narrow",system-ui,sans-serif}
h1{font-size:26px;font-weight:600;margin:0;letter-spacing:-.01em;text-wrap:balance}
h1 span{color:var(--muted);font-weight:400}
h2{font-size:13px;font-weight:600;margin:0 0 10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
.top{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:var(--chip);border-radius:999px;padding:2px 10px;font-size:12.5px;color:var(--ink)}
.pill{display:inline-flex;align-items:center;gap:7px;border-radius:999px;padding:4px 12px;font-size:13px;font-weight:500}
.pill::before{content:"";width:8px;height:8px;border-radius:50%;background:currentColor}
.pill.ok{background:var(--good-bg);color:var(--good)}.pill.wait{background:var(--chip);color:var(--arth)}
.pill.stale{background:var(--warn-bg);color:var(--warn)}.pill.bad{background:var(--bad-bg);color:var(--bad)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;background:var(--rule);border:1px solid var(--rule);border-radius:12px;overflow:hidden}
.kpi{background:var(--panel);padding:12px 14px;display:flex;flex-direction:column;gap:2px}
.kl{font-size:12px;color:var(--muted);letter-spacing:.02em}
.kv{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.2}
.ks{font-size:12px;color:var(--muted)}
.panel{background:var(--panel);border:1px solid var(--rule);border-radius:12px;padding:14px 16px}
.scroll{overflow-x:auto}
.cols{display:grid;grid-template-columns:1fr;gap:18px}@media (max-width:899px){.cols>*{min-width:0}}
@media (min-width:900px){.cols{grid-template-columns:1.25fr 1fr}.cols.even{grid-template-columns:1fr 1fr}}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:14px}
th,td{padding:6px 8px;border-bottom:1px solid var(--rule);text-align:left;white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
th{font-size:11.5px;color:var(--muted);font-weight:500;text-transform:uppercase;letter-spacing:.05em}
td.n,th.n{text-align:right}td.mono{font-family:"IBM Plex Mono",ui-monospace,Consolas,monospace;font-size:13px}
.pos{color:var(--good)}.neg{color:var(--bad)}.muted{color:var(--muted)}
.tag{display:inline-block;border-radius:6px;padding:0 7px;font-size:12px;font-weight:500}
.tag.new{background:var(--arth-soft);color:var(--arth)}.tag.hold{background:var(--chip);color:var(--muted)}
.tag.buy{background:var(--good-bg);color:var(--good)}.tag.sell{background:var(--bad-bg);color:var(--bad)}
svg{width:100%;height:auto;display:block}.grid{stroke:var(--rule);stroke-width:1}.base{stroke:var(--muted);stroke-width:1;stroke-dasharray:2 3}
.tick{fill:var(--muted);font-size:11px}.area{fill:var(--arth-soft);stroke:none}
.arth{fill:none;stroke:var(--arth);stroke-width:2.2}.fund{fill:none;stroke:var(--fund);stroke-width:1.6;stroke-dasharray:6 4}
.dot{fill:var(--arth)}.lab{font-size:12px;font-weight:600}.arthl{fill:var(--arth)}.fundl{fill:var(--fund)}
.empty{display:flex;flex-direction:column;gap:4px;padding:18px 4px;color:var(--muted)}.empty strong{color:var(--ink);font-weight:500}
ul.log{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px;font-size:14px}
ul.log li{display:grid;grid-template-columns:92px 1fr;gap:10px}ul.log .lv-warn{color:var(--warn)}ul.log .lv-error{color:var(--bad)}
.note{font-size:12.5px;color:var(--muted);max-width:75ch}
.nav{display:flex;gap:6px;flex-wrap:wrap}
.nav a{font-size:13px;padding:4px 12px;border-radius:999px;text-decoration:none;color:var(--ink);background:var(--chip)}
.nav a[aria-current]{background:var(--ink);color:var(--panel)}
ol.steps{margin:0;padding-left:22px;display:flex;flex-direction:column;gap:8px;font-size:14.5px;line-height:1.5;max-width:90ch}ol.steps b{font-weight:600}
p{margin:0}
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family='
         'IBM+Plex+Sans+Condensed:wght@400;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">')

INDEX_NAME = "Nifty Midcap150 Momentum 50"


def how_picked(params: dict, cap: float) -> str:
    """Short plain-language selection rules, filled from the ledger's own parameters so they cannot drift."""
    n = int(params.get("top_n", 15)); keep = int(round(float(params.get("buffer_mult", 2.0)) * n))
    uni = _group_in(int(params.get("universe_size", 1000))); tv = float(params.get("min_tv", 0)) / 1e7
    band = float(params.get("band", 0.25)) * 100; minpx = float(params.get("min_price", 30)); hist = int(params.get("min_history", 260))
    score = ("its return from 12 months ago to 1 month ago, divided by its volatility over the past year"
             if str(params.get("scorer")) == "121" else "a blend of 6- and 12-month risk-adjusted returns")
    overlay = (" When the index is below its 200-day average, only half the money is invested." if params.get("overlay")
               else " There is no market-timing switch: it stays fully invested.")
    steps = [
        ("Universe", f"Every NSE share priced at ₹{minpx:.0f} or more, listed for {hist}+ sessions"
                     + (f" and trading a median ₹{tv:.0f} crore or more a day over the last six months" if tv else "")
                     + f". The {uni} most traded of these make the list."),
        ("Score", f"Each stock is scored on {score}. Skipping the latest month avoids its short-term reversals; "
                  "dividing by volatility favours steady climbers over wild ones. Prices are adjusted for splits and bonuses."),
        ("Pick", f"The {n} highest scores, in equal amounts (about {_inr(cap / n)} each at the start)."),
        ("Rebalance", f"On the first trading day of each month, at the closing price, using the previous day's ranking. "
                      f"A holding stays while it ranks in the top {keep}; a new name enters only when one drops out. "
                      f"Amounts are reset only if a position is more than {band:.0f}% off its target." + overlay),
        ("Nothing else", "No stop-losses, profit targets, news, tips or mid-month trades. "
                         "A stock leaves only at a monthly rebalance."),
    ]
    lis = "".join(f"<li><b>{t}.</b> {html.escape(d)}</li>" for t, d in steps)
    return ("<section class='panel'><h2>How stocks are picked</h2>"
            f"<ol class='steps'>{lis}</ol>"
            "<p class='note' style='margin-top:10px'>Why it can work: stocks that have risen steadily over the past year "
            "tend to keep beating the market for several more months (the momentum effect, documented in India and abroad). "
            "It fails when market leadership flips suddenly.</p></section>")


STALE_JS = """<script>
(function(){var el=document.getElementById('status');if(!el)return;var g=Number(el.getAttribute('data-generated'));
if(!g)return;var h=(Date.now()-g)/36e5;if(h>50&&!el.classList.contains('bad')){el.className='pill stale';
el.textContent='No update for '+Math.floor(h/24)+' days: check the scheduled run';}})();
</script>"""


def write(db: Path, panel: Path, out_dir: Path | None = None, fragment: bool = False) -> Path:
    meta, days, fills, state, plans, events = _load(db)
    out_dir = Path(out_dir or OUT); out_dir.mkdir(parents=True, exist_ok=True)
    cap = float(meta.get("capital", 1_000_000))
    P = pd.read_pickle(panel)
    raw = P["RAW"].ffill().iloc[-1]
    pos = state.get("pos", {})
    eq = days.equity.iloc[-1] if len(days) else cap
    now = dt.datetime.now(dt.timezone.utc)
    data_through = meta.get("data_through") or P["RAW"].index[-1].date().isoformat()

    # holdings
    rows = []
    for s, q in sorted(pos.items(), key=lambda kv: -kv[1] * raw.get(kv[0], 0)):
        lots = state["lots"].get(s, [])
        cost = sum(l[0] * l[1] for l in lots) / max(1, sum(l[0] for l in lots))
        v = q * raw[s]
        rows.append(f"<tr><td>{html.escape(s)}</td><td class='n'>{_group_in(q)}</td><td class='n'>{_px(cost)}</td>"
                    f"<td class='n'>{_px(raw[s])}</td><td class='n'>{_inr(v)}</td><td class='n'>{v / eq * 100:.1f}%</td>"
                    f"<td class='n {'pos' if raw[s] >= cost else 'neg'}'>{_pct(raw[s] / cost - 1)}</td></tr>")
    holdings = ("<div class='scroll'><table><thead><tr><th>Stock</th><th class='n'>Shares</th><th class='n'>Avg cost</th>"
                "<th class='n'>Last</th><th class='n'>Value</th><th class='n'>Weight</th><th class='n'>P&amp;L</th></tr></thead><tbody>"
                + "".join(rows) + "</tbody></table></div>") if rows else \
        f"<div class='empty'><strong>No holdings yet.</strong><span>The first 15 buys fill at the close on {_d(meta.get('start'))}.</span></div>"

    # next rebalance target list
    plan_html = ""
    latest_day = days.index[-1].date().isoformat() if len(days) else None
    if len(plans) and (latest_day is None or str(plans.signal_date.iloc[0]) == latest_day):
        sig = plans.signal_date.iloc[0]
        pl = plans[plans.signal_date == sig]
        held = set(pos)
        fill_day = meta.get("start") if latest_day is None else None
        exits = sorted(held - set(pl.symbol))
        prev_start = CAL_prev(meta.get("start")) if fill_day else None
        if fill_day and prev_start and str(sig) < prev_start:
            note = (f"Indicative: ranked on the {_d(sig, '%d %b')} close. The final list is ranked on the "
                    f"{_d(prev_start, '%d %b')} close, and the orders fill at the close on {_d(fill_day)}.")
        else:
            note = (f"Ranked on the {_d(sig, '%d %b')} close. Orders fill at the next session's close"
                    + (f" ({_d(fill_day)})" if fill_day else "") + ".")
        plan_html = (f"<h2>Next rebalance · target list</h2><p class='note' style='margin-bottom:8px'>{note}</p>"
                     "<div class='scroll'><table><thead><tr><th class='n'>Rank</th><th>Stock</th><th class='n'>Target</th>"
                     "<th class='n'>Last</th><th></th></tr></thead><tbody>" + "".join(
                         f"<tr><td class='n'>{r.rank}</td><td>{html.escape(r.symbol)}</td><td class='n'>{_inr(r.target_value)}</td>"
                         f"<td class='n'>{_px(raw.get(r.symbol))}</td>"
                         f"<td><span class='tag {'hold' if r.symbol in held else 'new'}'>{'hold' if r.symbol in held else 'new'}</span></td></tr>"
                         for r in pl.itertuples()) + "</tbody></table></div>"
                     + (f"<p class='note' style='margin-top:8px'>Exits: {html.escape(', '.join(exits))}</p>" if exits else ""))

    # last rebalance
    lf = fills[fills.date == fills.date.max()] if len(fills) else fills
    orders = ("<div class='scroll'><table><thead><tr><th></th><th>Stock</th><th class='n'>Shares</th><th class='n'>Price</th>"
              "<th class='n'>Value</th><th class='n'>Charges</th><th>Why</th></tr></thead><tbody>" + "".join(
                  f"<tr><td><span class='tag {'sell' if r.side == 'SELL' else 'buy'}'>{r.side.lower()}</span></td><td>{html.escape(r.symbol)}</td>"
                  f"<td class='n'>{_group_in(int(r.qty))}</td><td class='n'>{_px(r.price)}</td><td class='n'>{_inr(r.value)}</td>"
                  f"<td class='n'>{_px(r.charges)}</td><td class='muted'>{html.escape(str(r.reason))}</td></tr>" for r in lf.itertuples())
              + "</tbody></table></div>") if len(lf) else "<div class='empty'><strong>No orders yet.</strong></div>"
    last_reb = f" · {_d(lf.date.iloc[0], '%d %b %Y')}" if len(lf) else ""

    # months
    monthly = "<div class='empty'><strong>Monthly returns appear after the first month-end.</strong></div>"
    if len(days) > 1:
        m = pd.DataFrame({"Arth": days.equity, "Fund": days.fund}).resample("ME").last()
        first = pd.DataFrame({"Arth": [cap], "Fund": [cap]}, index=[days.index[0] - pd.Timedelta(days=1)])
        m = pd.concat([first, m]).pct_change().dropna()
        monthly = ("<div class='scroll'><table><thead><tr><th>Month</th><th class='n'>Arth</th><th class='n'>Index</th>"
                   "<th class='n'>Gap</th></tr></thead><tbody>" + "".join(
                       f"<tr><td>{i:%b %Y}</td><td class='n {'pos' if r.Arth >= 0 else 'neg'}'>{_pct(r.Arth)}</td>"
                       f"<td class='n'>{_pct(r.Fund)}</td><td class='n {'pos' if r.Arth >= r.Fund else 'neg'}'>{_pct(r.Arth - r.Fund)}</td></tr>"
                       for i, r in m[::-1].iterrows()) + "</tbody></table></div>")

    # tax
    realised = [Realised(dt.date.fromisoformat(a), b, c, g, dd) for a, b, c, g, dd in state.get("realised", [])]
    tax = tax_by_year(realised)
    tax_html = "".join(f"<tr><td>FY {fy}</td><td class='n'>{_inr(v['short_term'])}</td><td class='n'>{_inr(v['long_term'])}</td>"
                       f"<td class='n'>{_inr(v['tax'])}</td><td class='n'>{_inr(-(v['carry_st'] + v['carry_lt'])) if v['carry_st'] + v['carry_lt'] < 0 else '–'}</td></tr>"
                       for fy, v in tax.items())
    tax_html = ("<div class='scroll'><table><thead><tr><th>Year</th><th class='n'>Short-term</th><th class='n'>Long-term</th>"
                "<th class='n'>Tax est.</th><th class='n'>Loss c/f</th></tr></thead><tbody>" + tax_html + "</tbody></table></div>") \
        if tax_html else "<div class='empty'><strong>No gains realised yet.</strong><span>Short-term 20% + 4% cess; long-term 12.5% over ₹1.25 lakh.</span></div>"

    ev = "".join(f"<li><span class='muted'>{_d(r.date, '%d %b %Y')}</span><span class='lv-{r.level}'>{html.escape(r.msg)}</span></li>"
                 for r in events.itertuples())

    # headline numbers
    k = lambda label, val, sub="", cls="": (f"<div class='kpi'><div class='kl'>{label}</div><div class='kv {cls}'>{val}</div>"
                                            f"<div class='ks'>{sub}</div></div>")
    if len(days):
        last = days.iloc[-1]; peak = days.equity.cummax().iloc[-1]
        mstart = days.equity[days.index < days.index[-1].to_period("M").start_time]
        base_m = mstart.iloc[-1] if len(mstart) else cap
        lead = (last.equity - last.fund) / cap
        kpis = (k("Equity", _inr(last.equity), f"{_pct(last.equity / cap - 1)} since {_d(meta['start'], '%d %b %Y')}",
                  "pos" if last.equity >= cap else "neg")
                + k("Same money in the index", _inr(last.fund), f"{_pct(last.fund / cap - 1)} · {INDEX_NAME}")
                + k("Lead over the index", _pct(lead), "rupee gap ÷ capital", "pos" if lead >= 0 else "neg")
                + k("This month", _pct(last.equity / base_m - 1), f"{days.index[-1]:%B} so far")
                + k("Drawdown", _pct(last.equity / peak - 1), f"from peak {_inr(peak)}")
                + k("Cash", _inr(last.cash), f"{last.cash / last.equity * 100:.1f}% · {int(last.positions)} holdings"))
    else:
        n_plan = len(plans[plans.signal_date == plans.signal_date.iloc[0]]) if len(plans) else 0
        kpis = (k("Paper capital", _inr(cap), "no real money moves")
                + k("First rebalance", _d(meta.get("start"), "%a %d %b"), "at the official close")
                + k("Target list", f"{n_plan} names" if n_plan else "–", "from the latest close")
                + k("Data through", _d(data_through, "%d %b %Y"), "NSE bhavcopy"))

    # status pill
    nxt = meta.get("next_rebalance") or (next_month_start(days.index[-1]) if len(days) else meta.get("start"))
    if nxt in ("None", ""):
        nxt = next_month_start(days.index[-1]) if len(days) else meta.get("start")
    run_ok = meta.get("run_status", "ok") == "ok"
    if not run_ok:
        status = ("bad", f"Last run failed: {meta.get('run_note', 'see log')}")
    elif not len(days):
        status = ("wait", f"Waiting for the first session · {_d(meta.get('start'), '%a %d %b')}")
    else:
        status = ("ok", f"Up to date · data through {_d(data_through, '%d %b')}" + (f" · rebalance {_d(nxt, '%a %d %b')}" if nxt else ""))
    gen_ms = int(now.timestamp() * 1000)

    params = json.loads(meta.get("params", "{}"))
    chips = [f"{html.escape(meta.get('dial', ''))} setting", f"{params.get('top_n')} stocks",
             "12-1 momentum" if str(params.get("scorer")) == "121" else f"score {html.escape(str(params.get('scorer')))}",
             f"most liquid {_group_in(int(params.get('universe_size', 0)))}",
             f"≥ ₹{params.get('min_tv', 0) / 1e7:.0f} cr/day traded" if params.get("min_tv") else None,
             f"trend overlay {'on' if params.get('overlay') else 'off'}", "monthly, first session",
             f"capital {_inr(cap)}"]
    chips_html = "".join(f"<span class='chip'>{c}</span>" for c in chips if c)
    how_html = how_picked(params, cap)
    code = days.code.iloc[-1] if len(days) else meta.get("code_at_init", "")
    runner = meta.get("runner", "server")
    gen_ist = now.astimezone(IST)

    nav = "" if fragment else ('<nav class="nav" aria-label="Sections"><a href="./" aria-current="page">Paper desk</a>'
                               '<a href="analyse.html">Analyse a stock</a></nav>')
    body = f"""<div class="wrap">
<header class="top"><h1>Arth <span>paper desk</span></h1>
<span id="status" class="pill {status[0]}" data-generated="{gen_ms}">{html.escape(status[1])}</span></header>
{nav}
<div class="chips">{chips_html}</div>
<section class="kpis">{kpis}</section>
<section class="panel"><h2>Equity against the {INDEX_NAME} index</h2>{_svg_chart(days, cap)}</section>
{how_html}
<div class="cols">
<section class="panel"><h2>Holdings</h2>{holdings}</section>
{('<section class="panel">' + plan_html + '</section>') if plan_html else '<section class="panel"><h2>Next rebalance</h2><div class="empty"><strong>' + (_d(nxt) if nxt else 'First session of next month') + '</strong><span>The target list appears the evening before.</span></div></section>'}
</div>
<section class="panel"><h2>Last rebalance{last_reb}</h2>{orders}</section>
<div class="cols even">
<section class="panel"><h2>Months</h2>{monthly}</section>
<section class="panel"><h2>Tax so far (estimate)</h2>{tax_html}</section>
</div>
<section class="panel"><h2>Log</h2><ul class="log">{ev or '<li><span></span><span class="muted">Nothing yet.</span></li>'}</ul></section>
<p class="note">Paper fills at NSE's official closing price ± 0.15%, with Upstox delivery charges (brokerage, STT 0.1% each side,
exchange, stamp, DP, GST). Index = the same capital put into the Nifty Midcap150 Momentum 50 index at the close on the start date (price index,
no costs or dividends): what a momentum index fund would have done. Signals use the previous session's close only. Updated {gen_ist:%a %d %b %Y, %H:%M} IST by the {html.escape(runner)} run ·
code {html.escape(str(code))}.</p>
</div>"""
    head = f"<title>Arth paper desk</title>\n{FONTS}\n<style>{CSS}</style>\n"
    page = (head + body + STALE_JS) if fragment else \
        (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"{head}</head><body>{body}{STALE_JS}</body></html>")
    path = out_dir / "index.html"
    path.write_text(page, encoding="utf-8")
    (out_dir / "latest.txt").write_text(summary(db) + "\n", encoding="utf-8")
    return path
