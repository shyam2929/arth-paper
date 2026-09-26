"""
Paper trading: the production signal and order planner, filled on paper at NSE's official closing price.

Why this is a faithful rehearsal: the backtest trades at the close with 0.15% slippage, so the paper broker
does the same, using the exchange's own closing price file. Every rupee is booked in a SQLite ledger with
FIFO tax lots, splits and bonuses applied to share counts, and a shadow ledger that puts the same capital
into the Midcap150 Momentum 50 index on the start date.

Processing is idempotent and in date order: a missed evening is caught up next time, with identical results,
because fills use published closing prices and the code version is recorded in the ledger.

    python -m arth.paper init --start 2026-10-01 --capital 1000000
    python -m arth.paper daily          # update data, process new sessions, write reports, notify
    python -m arth.paper status
"""
from __future__ import annotations
import argparse, datetime as dt, fcntl, json, math, sqlite3, subprocess, sys
from pathlib import Path
import pandas as pd

from arth import config as CFG
from arth.data import calendar as CAL
from arth.exec.orders import make_tag
from arth.ledger import fees as F
from arth.ledger.lots import LotBook, Lot, tax_by_year
from arth.portfolio.diff import plan_orders
from arth.risk.pretrade import check_order, Quote, Limits
from arth.strategy import signals as S

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data/paper.sqlite"
PANEL = ROOT / "data/panel_nse.pkl"
SLIP = 0.0015

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS days(date TEXT PRIMARY KEY, equity REAL, cash REAL, mv REAL, fund REAL,
    positions INT, rebalance INT, code TEXT);
CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, tag TEXT UNIQUE, symbol TEXT,
    side TEXT, qty INT, price REAL, value REAL, charges REAL, stt REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS state(date TEXT PRIMARY KEY, json TEXT);
CREATE TABLE IF NOT EXISTS plans(signal_date TEXT, symbol TEXT, target_value REAL, rank INT,
    PRIMARY KEY(signal_date, symbol));
CREATE TABLE IF NOT EXISTS events(ts TEXT, date TEXT, level TEXT, msg TEXT);
"""


def connect(db: Path = DB) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    return con


def code_version() -> str:
    """git describe when run from a checkout; the VERSION file shipped with a code bundle otherwise."""
    v = ""
    if (ROOT / ".git").exists():
        try:
            v = subprocess.run(["git", "-C", str(ROOT), "describe", "--always", "--dirty", "--tags"],
                               capture_output=True, text=True).stdout.strip()
        except Exception:
            v = ""
    if not v and (ROOT / "VERSION").exists():
        v = (ROOT / "VERSION").read_text().strip()
    return v or "unknown"


def meta(con, key, default=None):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def log(con, date, level, msg):
    con.execute("INSERT INTO events VALUES(?,?,?,?)", (dt.datetime.now().isoformat(timespec="seconds"), date, level, msg))


# ---- state -----------------------------------------------------------------------------------------
def load_state(con) -> tuple[str | None, dict]:
    r = con.execute("SELECT date, json FROM state ORDER BY date DESC LIMIT 1").fetchone()
    if not r:
        return None, {}
    return r[0], json.loads(r[1])


def book_from(state: dict) -> LotBook:
    b = LotBook()
    for sym, lots in state.get("lots", {}).items():
        for q, c, d in lots:
            b.lots[sym].append(Lot(q, c, dt.date.fromisoformat(d)))
    return b


def book_to(b: LotBook) -> dict:
    return {s: [[l.qty, l.unit_cost, l.date.isoformat()] for l in dq] for s, dq in b.lots.items() if dq}


def _num(x):
    try:
        x = float(x)
        return x if math.isfinite(x) and x > 0 else None
    except (TypeError, ValueError):
        return None


def _scale(book: LotBook, pos: dict, s_: str, fct: float, before: dt.date | None = None) -> tuple[int, int, float]:
    """Apply a split/bonus factor to one holding: shares x 1/fct, unit cost x fct. With `before`, only lots
    bought before that date are affected (shares bought on or after an ex-date are already post-action).
    Returns (old shares, new shares, fractional share left over, paid out in cash by the caller)."""
    lots = [l for l in book.lots[s_] if before is None or l.date < before]
    old_aff = sum(l.qty for l in lots)
    exact = old_aff / fct; new_aff = int(math.floor(exact + 1e-9))
    for lot in lots:
        lot.qty = int(math.floor(lot.qty / fct + 1e-9)); lot.unit_cost *= fct
    gap = new_aff - sum(l.qty for l in lots)
    if gap and lots:
        lots[0].qty += gap
    old = pos[s_]; pos[s_] = old - old_aff + new_aff
    return old, pos[s_], exact - new_aff


def remap_renamed(con, state: dict, P: dict, d: str) -> bool:
    """Entities are named after their latest symbol, so a symbol change renames a column overnight. Move a
    holding whose symbol vanished to the entity that used it (panel META lists every symbol an entity had)."""
    cols = set(P["RAW"].columns); meta = P.get("META"); changed = False
    for s_ in list(state.get("pos", {})):
        if s_ in cols or meta is None:
            continue
        hits = [e for e, syms in meta["symbols"].items() if e in cols and s_ in str(syms).split("|")]
        if len(hits) != 1:
            raise SystemExit(f"held symbol {s_} is not in the panel and maps to {hits or 'nothing'}; fix by hand")
        new = hits[0]
        state["pos"][new] = state["pos"].get(new, 0) + state["pos"].pop(s_)
        state["lots"][new] = state["lots"].get(new, []) + state["lots"].pop(s_, [])
        state["ca_done"] = [(new + k[len(s_):]) if k.startswith(s_ + "@") else k for k in state.get("ca_done", [])]
        log(con, d, "warn", f"symbol change: holding {s_} is now {new}"); changed = True
    return changed


def late_corporate_actions(con, state: dict, P: dict, last: pd.Timestamp, lookback: int = 15) -> bool:
    """A split or bonus that NSE listed only after its ex-date was processed would leave the share count
    unadjusted for good. Re-check recent processed sessions and apply any factor not yet applied."""
    fac = P.get("FAC")
    if fac is None or not state.get("pos"):
        return False
    done = set(state.get("ca_done", [])); book = book_from(state); pos = dict(state["pos"]); changed = False
    px = P["RAW"].loc[:last].ffill().iloc[-1]
    for t in fac.index[fac.index <= last][-lookback:]:
        for s_ in list(pos):
            if s_ not in fac.columns:
                continue
            fct = fac.at[t, s_]
            key = f"{s_}@{t.date()}"
            if pd.isna(fct) or fct == 1.0 or key in done:
                continue
            old, new, frac = _scale(book, pos, s_, float(fct), before=t.date())
            state["cash"] = float(state["cash"]) + frac * float(px[s_])
            done.add(key); changed = True
            log(con, last.date().isoformat(), "warn",
                f"{s_}: corporate action of {t.date()} applied late (factor {fct:.4f}), {old} -> {new} shares")
    if changed:
        state["pos"] = pos; state["lots"] = book_to(book); state["ca_done"] = sorted(done)[-300:]
    return changed


def fit_to_cash(orders, cash: float, reserve: float = 0.003):
    """A broker will not let buys exceed cash. Sale proceeds count (same-day credit); if the buys still
    don't fit, shrink every buy in proportion, keeping `reserve` of the order value for charges."""
    from dataclasses import replace
    sells = sum(o.qty * o.ref_price * (1 - SLIP) for o in orders if o.side == "SELL")
    buys = sum(o.qty * o.ref_price * (1 + SLIP) for o in orders if o.side == "BUY")
    room = (cash + sells) * (1 - reserve)
    if buys <= room or buys <= 0:
        return orders
    k = max(0.0, room / buys)
    out = []
    for o in orders:
        if o.side == "BUY":
            q = int(math.floor(o.qty * k))
            if q > 0:
                out.append(replace(o, qty=q))
        else:
            out.append(o)
    return out


# ---- one session ----------------------------------------------------------------------------------
def process_day(con, P: dict, f: S.Features, t: pd.Timestamp, prev_t: pd.Timestamp, state: dict,
                params, capital: float, cash_yield: float = 0.0) -> dict:
    """Process session t. prev_t is the previous trading session (the signal date on rebalance days)."""
    raw = P["RAW"]; fac = P.get("FAC")
    px = raw.loc[:t].ffill().iloc[-1]
    first = not state
    if first:
        state = {"cash": capital, "pos": {}, "lots": {}, "realised": [], "fund_units": None, "peak": capital}
    cash = float(state["cash"]); pos = {k: int(v) for k, v in state["pos"].items()}
    book = book_from(state)
    d = t.date().isoformat()
    # corporate actions on held names: split/bonus factor f scales shares by 1/f and unit cost by f
    ca_done = list(state.get("ca_done", []))
    if fac is not None and t in fac.index:
        for s_ in list(pos):
            fct = fac.at[t, s_] if s_ in fac.columns else 1.0
            if pd.notna(fct) and fct != 1.0 and f"{s_}@{t.date()}" not in ca_done:
                old, new, frac = _scale(book, pos, s_, float(fct))
                cash += frac * px[s_]                    # fractional entitlement paid in cash
                ca_done.append(f"{s_}@{t.date()}")
                log(con, d, "info", f"{s_}: corporate action factor {fct:.4f}, {old} -> {new} shares")
    if not first and cash_yield:
        cash += max(0.0, cash) * cash_yield * (t - prev_t).days / 365
    # rebalance on the first session of each month; the first session of the paper run also invests
    rebalance = first or CAL.is_first_session_of_month(t.date(), prev_t.date())
    n_fills = 0
    if rebalance:
        sig = prev_t                                      # signals use the previous close only
        mv = sum(q * px[s_] for s_, q in pos.items())
        eq0 = cash + mv                                   # equity before any order, fixed for all checks
        tv = S.targets(f, sig, list(pos), eq0, params)
        orders = fit_to_cash(plan_orders(pos, tv, px.to_dict(), params.band), cash)
        month = t.strftime("%Y%m")
        tvv = f.tv126.loc[sig]
        for i, o in enumerate(orders, 1):
            lim = o.ref_price * (1 + SLIP if o.side == "BUY" else 1 - SLIP)
            why = check_order(symbol=o.symbol, side=o.side, qty=o.qty, limit_price=lim, product="D",
                              target_list=set(tv), target_value=tv.get(o.symbol), equity=eq0,
                              quote=Quote(o.ref_price, 0.0), median_tv20=_num(tvv.get(o.symbol)),
                              limits=Limits(max_order_pct_equity=max(0.10, 1.5 / params.top_n)), now=0.0)
            if why and "median traded value" in why:
                log(con, d, "warn", f"{o.symbol}: {why} (paper fills it; live splits it across sessions)")
            elif why:
                log(con, d, "warn", f"rejected {o.side} {o.qty} {o.symbol}: {why}")
                continue
            tag = make_tag(month, o.side, o.symbol, i)
            value = o.qty * lim
            ch = F.charges(o.side, value)
            stt = round(value * F.CURRENT.stt, 2)
            if o.side == "SELL":
                cash += value - ch
                book.sell(o.symbol, o.qty, value, ch, stt, t.date())
                pos[o.symbol] -= o.qty
                if pos[o.symbol] == 0:
                    del pos[o.symbol]
            else:
                cash -= value + ch
                book.buy(o.symbol, o.qty, value, ch, stt, t.date())
                pos[o.symbol] = pos.get(o.symbol, 0) + o.qty
            con.execute("INSERT OR IGNORE INTO fills(date,tag,symbol,side,qty,price,value,charges,stt,reason) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)", (d, tag, o.symbol, o.side, o.qty, lim, value, ch, stt, o.reason))
            n_fills += 1
        log(con, d, "info", f"rebalance: {n_fills} fills, signal date {sig.date()}")
    mv = sum(q * px[s_] for s_, q in pos.items())
    equity = cash + mv
    # shadow fund: the same capital into the momentum index on the start date
    lvl = P["IDX"]["NiftyM150Momntm50"].loc[:t].ffill().iloc[-1]
    fund_units = state.get("fund_units") or capital / lvl
    fund = fund_units * lvl
    state = {"cash": cash, "pos": pos, "lots": book_to(book),
             "realised": state.get("realised", []) + [[r.date.isoformat(), r.symbol, r.qty, r.gain, r.days] for r in book.realised],
             "fund_units": fund_units, "peak": max(state.get("peak", capital), equity), "ca_done": ca_done[-300:]}
    con.execute("INSERT OR REPLACE INTO days VALUES(?,?,?,?,?,?,?,?)",
                (d, equity, cash, mv, fund, len(pos), int(rebalance), code_version()))
    con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (d, json.dumps(state)))
    return state


def preview(con, f: S.Features, t: pd.Timestamp, state: dict, params, px: pd.Series, start=None, force=False):
    """If the next session is a rebalance (new month, or the paper run's first day), store the target list
    computed from today's close. `force` stores it anyway (an indicative list before the paper run starts)."""
    nxt = CAL.next_session(t.date())
    first_day = start is not None and pd.Timestamp(nxt) == pd.Timestamp(start)
    if not (CAL.is_first_session_of_month(nxt, t.date()) or first_day or force):
        return None
    if force and not first_day and start is not None:
        nxt = pd.Timestamp(start).date()
    pos = state.get("pos", {})
    eq = state["cash"] + sum(q * px[s] for s, q in pos.items())
    tv = S.targets(f, t, list(pos), eq, params)
    names = S.universe(f, t, params.universe_size, params.min_price, params.min_history, params.min_tv)
    ranked = S.rank(f, t, names, params.scorer)
    rk = {s: i + 1 for i, s in enumerate(ranked.index)}
    con.execute("DELETE FROM plans WHERE signal_date=?", (t.date().isoformat(),))
    for s, v in tv.items():
        con.execute("INSERT INTO plans VALUES(?,?,?,?)", (t.date().isoformat(), s, v, rk.get(s)))
    return nxt


def run(db: Path = DB, panel: Path = PANEL, until: str | None = None, params=None) -> dict:
    con = connect(db)
    start = meta(con, "start")
    if not start:
        raise SystemExit("not initialised: python -m arth.paper init --start YYYY-MM-DD --capital N")
    start = pd.Timestamp(start); capital = float(meta(con, "capital"))
    params = params or CFG.strategy_params()
    P = pd.read_pickle(panel)
    cols = list(P["C"].columns)
    f = S.Features.build(P["C"][cols], P["V"][cols], P["IDX"]["Nifty_500"], unadjusted=P["RAW"][cols])
    dates = P["C"].index
    if until:
        dates = dates[dates <= pd.Timestamp(until)]
    last, state = load_state(con)
    if state:                                             # overnight repairs before new sessions
        fixed = remap_renamed(con, state, P, last)
        fixed = late_corporate_actions(con, state, P, pd.Timestamp(last)) or fixed
        if fixed:
            con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (last, json.dumps(state))); con.commit()
    todo = [t for t in dates if t >= start and (last is None or t > pd.Timestamp(last))]
    done = []
    for t in todo:
        i = dates.get_loc(t)
        if i == 0:
            raise SystemExit("start date must have at least one earlier session in the panel")
        state = process_day(con, P, f, t, dates[i - 1], state, params, capital)
        con.commit(); done.append(t.date().isoformat()); last = t.date().isoformat()
    nxt = None
    if state:
        t = pd.Timestamp(last)
        nxt = preview(con, f, t, state, params, P["RAW"].loc[:t].ffill().iloc[-1]); con.commit()
    elif len(dates) and dates[-1] < start:                 # before the first session: preview day one
        t = dates[-1]
        con.execute("DELETE FROM plans")                   # keep only the latest indicative list
        nxt = preview(con, f, t, {"cash": capital, "pos": {}}, params, P["RAW"].loc[:t].ffill().iloc[-1], start, force=True)
        con.commit()
    return {"processed": done, "last": last, "next_rebalance": str(nxt) if nxt else None}


def init(start: str, capital: float, db: Path = DB, force: bool = False):
    con = connect(db)
    if meta(con, "start") and not force:
        raise SystemExit(f"already initialised (start {meta(con, 'start')}); use --force to reset")
    if force:
        for tbl in ("meta", "days", "fills", "state", "plans", "events"):
            con.execute(f"DELETE FROM {tbl}")
    p = CFG.strategy_params()
    for k, v in {"start": start, "capital": str(capital), "dial": CFG.load()["dial"], "params": json.dumps(p.__dict__),
                 "code_at_init": code_version(), "created": dt.datetime.now().isoformat(timespec="seconds")}.items():
        con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))
    con.commit()
    print("paper ledger initialised:", start, capital, p)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="arth.paper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a_init = sub.add_parser("init"); a_init.add_argument("--start", required=True)
    a_init.add_argument("--capital", type=float, default=1_000_000); a_init.add_argument("--force", action="store_true")
    a_boot = sub.add_parser("bootstrap", help="first install: fetch history since --since, then init")
    a_boot.add_argument("--since", default="2024-06-01"); a_boot.add_argument("--start", default="2026-10-01")
    a_boot.add_argument("--capital", type=float, default=1_000_000)
    a_daily = sub.add_parser("daily"); a_daily.add_argument("--no-update", action="store_true")
    a_daily.add_argument("--until")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "init":
        init(a.start, a.capital, force=a.force); return 0
    if a.cmd == "bootstrap":
        from arth.data.update import update, DataNotReady
        try:
            last = update(since=a.since, fo=False)
        except DataNotReady:
            last = None                                    # today's file not out yet; history is in place
        con = connect()
        if not meta(con, "start"):
            init(a.start, a.capital)
        print("bootstrap done; data up to", last); return 0
    lock = open(ROOT / "data/.paper.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    if a.cmd == "daily":
        from arth.ops import report, notify
        if not a.no_update:
            from arth.data.update import update, DataNotReady
            try:
                update(fo=False)
            except DataNotReady as e:
                print("data not ready:", e); return 2          # the timer retries later tonight
            except Exception as e:
                notify.send(f"Arth: data update FAILED - {e}", title="Arth alert"); raise
        res = run(until=a.until)
        path = report.write(DB, PANEL)
        notify.send(report.summary(DB), attach=str(path) if res["processed"] else None)
        print(json.dumps(res), "report:", path)
        return 0
    if a.cmd == "status":
        from arth.ops import report
        print(report.summary(DB)); return 0


if __name__ == "__main__":
    sys.exit(main())
