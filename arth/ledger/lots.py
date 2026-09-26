"""
FIFO tax lots and Indian capital-gains tax for listed equity (STT paid), tax year 2026-27 rules:
  short-term (held <= 12 months): 20%; long-term: 12.5% above Rs 1.25 lakh a year; plus 4% cess.
  Short-term losses offset short- or long-term gains; long-term losses offset only long-term gains;
  unabsorbed losses carry forward 8 years (tracked here without expiry, a small simplification).
  Cost includes brokerage and charges but not STT, which the law does not allow as a deduction.
"""
from __future__ import annotations
from collections import defaultdict, deque
from dataclasses import dataclass, field
import datetime as dt


@dataclass
class Lot:
    qty: int
    unit_cost: float
    date: dt.date


@dataclass
class Realised:
    date: dt.date
    symbol: str
    qty: int
    gain: float
    days: int

    @property
    def long_term(self) -> bool:
        return self.days > 365


def fy_of(d: dt.date) -> str:
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{str(y + 1)[-2:]}"


@dataclass
class LotBook:
    lots: dict = field(default_factory=lambda: defaultdict(deque))
    realised: list = field(default_factory=list)

    def buy(self, symbol: str, qty: int, value: float, charges: float, stt: float, d: dt.date):
        self.lots[symbol].append(Lot(qty, (value + charges - stt) / qty, d))

    def sell(self, symbol: str, qty: int, value: float, charges: float, stt: float, d: dt.date) -> float:
        net_unit = (value - charges + stt) / qty
        left = qty; total = 0.0
        dq = self.lots[symbol]
        while left > 0 and dq:
            lot = dq[0]; take = min(left, lot.qty)
            g = take * (net_unit - lot.unit_cost)
            self.realised.append(Realised(d, symbol, take, g, (d - lot.date).days))
            total += g; lot.qty -= take; left -= take
            if lot.qty == 0:
                dq.popleft()
        if left > 0:
            raise ValueError(f"sold {qty} {symbol} but only {qty - left} in lots")
        return total

    def position(self, symbol: str) -> int:
        return sum(l.qty for l in self.lots[symbol])


def tax_by_year(realised: list[Realised], stcg=0.20, ltcg=0.125, exempt=125_000, cess=0.04) -> dict[str, dict]:
    by: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for r in realised:
        by[fy_of(r.date)][1 if r.long_term else 0] += r.gain
    out = {}
    carry_st = carry_lt = 0.0
    for fy in sorted(by):
        st, lt = by[fy]
        st += carry_st; lt += carry_lt; carry_st = carry_lt = 0.0
        if st < 0 < lt:
            off = min(-st, lt); st += off; lt -= off
        if st < 0: carry_st, st = st, 0.0
        if lt < 0: carry_lt, lt = lt, 0.0
        tax = (st * stcg + max(0.0, lt - exempt) * ltcg) * (1 + cess)
        out[fy] = dict(short_term=st, long_term=lt, tax=tax, carry_st=carry_st, carry_lt=carry_lt)
    return out
