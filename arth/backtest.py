"""
Backtest runner built from the production pieces: signals.targets -> diff.plan_orders -> fills -> ledger.
The live and paper paths call the same `targets` and `plan_orders`; only the fill step differs.
"""
from __future__ import annotations
import datetime as dt
from dataclasses import dataclass, field
import pandas as pd

from arth.config import StrategyParams
from arth.ledger import fees as F
from arth.ledger.lots import LotBook
from arth.portfolio.diff import plan_orders
from arth.strategy import signals as S


@dataclass
class Result:
    equity: pd.Series
    trades: list = field(default_factory=list)
    fees: float = 0.0
    lots: LotBook | None = None


def rebalance_days(dates: pd.DatetimeIndex, start, end) -> set:
    ds = dates[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
    s = pd.Series(ds, index=ds)
    return set(s.groupby(s.dt.to_period("M")).first().tolist())


def run(f: S.Features, p: StrategyParams, start="2018-02-01", end=None, capital=1_000_000.0,
        slippage=0.0015, cash_yield=0.05, schedule: F.DeliverySchedule = F.CURRENT) -> Result:
    dates = f.close.index
    end = end or dates[-1]
    days = dates[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
    rb = rebalance_days(dates, start, end)
    cash = float(capital); pos: dict[str, int] = {}
    book = LotBook(); eq = []; trades = []; fees_paid = 0.0; prev = None
    for t in days:
        px = f.close.loc[t]
        if prev is not None:
            cash += cash * (cash_yield if cash > 0 else 0.1825) * (t - prev).days / 365.0
        if t in rb:
            s_ = dates[dates.get_loc(t) - 1]
            mv = sum(q * px[s] for s, q in pos.items())
            tv = S.targets(f, s_, list(pos), cash + mv, p)
            for o in plan_orders(pos, tv, px.to_dict(), p.band):
                d = t.date()
                if o.side == "SELL":
                    v = o.qty * o.ref_price * (1 - slippage); c = F.charges("SELL", v, schedule)
                    cash += v - c; fees_paid += c
                    book.sell(o.symbol, o.qty, v, c, round(v * schedule.stt, 2), d)
                    pos[o.symbol] -= o.qty
                    if pos[o.symbol] == 0:
                        del pos[o.symbol]
                else:
                    v = o.qty * o.ref_price * (1 + slippage); c = F.charges("BUY", v, schedule)
                    cash -= v + c; fees_paid += c
                    book.buy(o.symbol, o.qty, v, c, round(v * schedule.stt, 2), d)
                    pos[o.symbol] = pos.get(o.symbol, 0) + o.qty
                trades.append((t, o.symbol, o.side, o.qty, o.ref_price, o.reason))
        eq.append((t, cash + sum(q * px[s] for s, q in pos.items())))
        prev = t
    e = pd.Series([x[1] for x in eq], index=[x[0] for x in eq])
    return Result(e, trades, fees_paid, book)


def cagr(e: pd.Series) -> float:
    yrs = (e.index[-1] - e.index[0]).days / 365.25
    return (e.iloc[-1] / e.iloc[0]) ** (1 / yrs) - 1
