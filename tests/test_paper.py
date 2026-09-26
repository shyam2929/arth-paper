"""Paper engine: calendar, split handling, idempotency, cash discipline, and replay against the backtest."""
import datetime as dt, os, sqlite3
import numpy as np
import pandas as pd
import pytest

from arth import paper
from arth.config import StrategyParams
from arth.data import calendar as CAL
from arth.portfolio.diff import Order


def test_calendar_2026():
    assert not CAL.is_session(dt.date(2026, 10, 2))                 # Gandhi Jayanti
    assert CAL.next_session(dt.date(2026, 9, 30)) == dt.date(2026, 10, 1)
    assert CAL.is_first_session_of_month(dt.date(2026, 10, 1), dt.date(2026, 9, 30))
    assert CAL.next_session(dt.date(2026, 10, 30)) == dt.date(2026, 11, 2)
    assert CAL.next_session(dt.date(2026, 11, 6)) == dt.date(2026, 11, 9)   # skips Muhurat Sunday


def test_fit_to_cash_scales_buys_only():
    orders = [Order("A", "SELL", 10, 100.0, "exit"), Order("B", "BUY", 100, 100.0, "entry"), Order("C", "BUY", 100, 100.0, "entry")]
    out = paper.fit_to_cash(orders, cash=9_000)
    buys = sum(o.qty * o.ref_price for o in out if o.side == "BUY")
    assert buys <= (9_000 + 1_000) and [o.symbol for o in out if o.side == "SELL"] == ["A"]


def _synthetic_panel(tmp_path, split_day=330):
    rng = np.random.default_rng(3)
    dates = pd.bdate_range("2024-01-01", periods=420)
    names = [f"S{i:02d}" for i in range(30)]
    drift = np.linspace(-0.0005, 0.0015, len(names)); drift[-1] = 0.004     # S29 trends hardest, so it is held
    rets = rng.normal(drift, 0.015, size=(len(dates), len(names)))
    adj = pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), index=dates, columns=names)
    raw = adj.copy(); fac = pd.DataFrame(1.0, index=dates, columns=names)
    # S29 (strongest drift, surely held) splits 1:5 on split_day: raw prices before it are 5x adjusted
    raw.iloc[:split_day, -1] *= 5; fac.iloc[split_day, -1] = 0.2
    vol = pd.DataFrame(1e7, index=dates, columns=names) / adj
    idx = pd.DataFrame({"Nifty_500": adj.mean(axis=1) * 100, "NiftyM150Momntm50": adj.mean(axis=1) * 100})
    P = {"C": adj, "V": vol, "RAW": raw, "FAC": fac, "IDX": idx}
    p = tmp_path / "panel.pkl"; pd.to_pickle(P, p)
    return p, dates


def test_split_keeps_value_and_rerun_is_noop(tmp_path):
    panel, dates = _synthetic_panel(tmp_path)
    db = tmp_path / "paper.sqlite"
    paper.init(str(dates[300].date()), 1_000_000, db=db)
    params = StrategyParams(top_n=5, overlay=False, universe_size=30, min_price=0, min_history=260, scorer="121")
    r1 = paper.run(db=db, panel=panel, params=params)
    con = sqlite3.connect(db)
    d = pd.read_sql("select date, equity, cash from days", con, parse_dates=["date"]).set_index("date")
    # value continuity across the split: equity change that day equals the portfolio's adjusted move
    t = dates[330]; i = d.index.get_loc(t)
    jump = d.equity.iloc[i] / d.equity.iloc[i - 1] - 1
    assert abs(jump) < 0.08
    ev = pd.read_sql("select msg from events where msg like '%corporate action%'", con)
    assert len(ev) == 1 and "S29" in ev.msg.iloc[0]
    assert (d.cash > -1).all()                     # never borrows
    r2 = paper.run(db=db, panel=panel, params=params)
    assert r2["processed"] == []
    d2 = pd.read_sql("select equity from days", con)
    assert len(d2) == len(d)


REAL = os.path.join(os.path.dirname(__file__), "..", "data", "panel_nse.pkl")


@pytest.mark.skipif(not os.path.exists(REAL), reason="NSE panel not built")
def test_replay_matches_backtest(tmp_path):
    from arth import backtest as B
    from arth.config import strategy_params
    from arth.strategy.signals import Features
    db = tmp_path / "p.sqlite"
    paper.init("2025-06-02", 1_000_000, db=db)
    paper.run(db=db, panel=REAL)
    d = pd.read_sql("select date, equity from days", sqlite3.connect(db), parse_dates=["date"]).set_index("date").equity
    P = pd.read_pickle(REAL); cols = list(P["C"].columns)
    f = Features.build(P["C"][cols], P["V"][cols], P["IDX"]["Nifty_500"], unadjusted=P["RAW"][cols])
    bt = B.run(f, strategy_params(), start="2025-06-02", cash_yield=0.0).equity.reindex(d.index)
    assert abs(d.iloc[-1] / bt.iloc[-1] - 1) < 0.02
