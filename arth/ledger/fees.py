"""
Upstox equity-delivery charges (NSE), current as of September 2026 (upstox.com/brokerage-charges):
  brokerage     min(Rs 20, 2.5% of order value) per executed order
  STT           0.1% on buy and sell
  exchange      0.00307% of turnover (NSE, from 1 Mar 2026; includes IPFT)
  SEBI fee      Rs 10 per crore
  stamp duty    0.015% on buys
  GST           18% on brokerage + exchange + SEBI fee + DP charge
  DP charge     Rs 20 + GST per stock per sell day
`LEGACY` reproduces the research engine's older schedule for the golden replay.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class DeliverySchedule:
    brokerage_cap: float = 20.0
    brokerage_pct: float = 0.025
    stt: float = 0.001
    exchange: float = 0.0000307
    sebi: float = 0.000001
    stamp_buy: float = 0.00015
    gst: float = 0.18
    dp_sell: float = 20.0          # before GST
    dp_gst_included: bool = False
    ipft: float = 0.0              # folded into `exchange` in the current schedule


CURRENT = DeliverySchedule()
LEGACY = DeliverySchedule(exchange=0.0000297, dp_sell=21.83, dp_gst_included=True, ipft=0.000001)


def charges(side: str, value: float, sch: DeliverySchedule = CURRENT) -> float:
    if value <= 0:
        return 0.0
    side = side.upper()
    brokerage = min(sch.brokerage_cap, round(value * sch.brokerage_pct, 2))
    stt = round(value * sch.stt, 2)
    exch = round(value * sch.exchange, 2)
    sebi = round(value * sch.sebi, 2)
    stamp = round(value * sch.stamp_buy, 2) if side == "BUY" else 0.0
    ipft = round(value * sch.ipft, 2)
    dp = 0.0
    if side == "SELL":
        dp = sch.dp_sell
    taxable = brokerage + exch + sebi + (0.0 if sch.dp_gst_included else dp)
    gst = round(taxable * sch.gst, 2)
    return round(brokerage + stt + exch + sebi + stamp + gst + dp + ipft, 2)
