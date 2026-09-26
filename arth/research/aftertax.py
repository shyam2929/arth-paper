"""After-tax comparison of an Arth backtest (FIFO lots) with a buy-and-hold index fund."""
from __future__ import annotations
import pandas as pd

from arth.ledger.lots import tax_by_year

CESS = 1.04


def after_tax_equity(equity: pd.Series, book, pay_month_day=(3, 15)) -> pd.Series:
    """Deduct each financial year's tax as a fraction of equity on the first session on/after 15 March
    (a stand-in for quarterly advance tax). Returns the after-tax equity path (liquidation not included)."""
    taxes = tax_by_year(book.realised)
    frac = pd.Series(1.0, index=equity.index)
    for fy, t in taxes.items():
        end_year = int(fy[:4]) + 1
        pay = pd.Timestamp(end_year, *pay_month_day)
        if pay > equity.index[-1]:
            pay = equity.index[-1]
        d = equity.index[equity.index >= pay][0]
        frac.loc[d] *= 1 - t["tax"] / equity.loc[d]
    return equity * frac.cumprod(), taxes


def liquidation_tax(book, last_prices: pd.Series, end: pd.Timestamp, scale: float = 1.0) -> float:
    st = lt = 0.0
    for sym, dq in book.lots.items():
        for lot in dq:
            g = lot.qty * (last_prices[sym] - lot.unit_cost)
            if (end.date() - lot.date).days > 365:
                lt += g
            else:
                st += g
    return scale * (max(0.0, st) * 0.20 * CESS + max(0.0, lt - 125_000) * 0.125 * CESS)


def fund_after_tax(level: pd.Series, capital: float, expense: float = 0.0) -> float:
    """Buy and hold, pay LTCG at the end. `expense` = annual expense ratio; left at 0 because the price index also omits dividends (~offsetting)."""
    yrs = (level.index[-1] - level.index[0]).days / 365.25
    end = capital * level.iloc[-1] / level.iloc[0] * (1 - expense) ** yrs
    gain = end - capital
    return end - max(0.0, gain - 125_000) * 0.125 * CESS


def cagr_between(v0: float, v1: float, d0, d1) -> float:
    yrs = (pd.Timestamp(d1) - pd.Timestamp(d0)).days / 365.25
    return (v1 / v0) ** (1 / yrs) - 1
