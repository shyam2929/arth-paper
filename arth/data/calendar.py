"""
NSE trading calendar.

Past sessions come from Upstox's public Nifty 50 daily candles (a candle exists only on a trading day);
today's session from the intraday endpoint. Future sessions use NSE's published holiday list
(circular CMTR71775 for 2026). Add each year's list when NSE publishes it in December.
"""
from __future__ import annotations
import datetime as dt
import requests

HOLIDAYS = {
    # NSE capital-market trading holidays 2026 (weekdays)
    "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01",
    "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24",
    "2026-12-25",
}
SPECIAL_SESSIONS = {"2026-11-08"}          # Muhurat trading (Sunday); a short evening session
NIFTY = "NSE_INDEX%7CNifty%2050"


def is_session(d: dt.date) -> bool:
    iso = d.isoformat()
    if iso in SPECIAL_SESSIONS:
        return True
    return d.weekday() < 5 and iso not in HOLIDAYS


def next_session(d: dt.date) -> dt.date:
    n = d + dt.timedelta(days=1)
    while not is_session(n) or n.isoformat() in SPECIAL_SESSIONS:   # Muhurat is never a rebalance day
        n += dt.timedelta(days=1)
    return n


def is_first_session_of_month(d: dt.date, prev_session: dt.date) -> bool:
    return (d.year, d.month) != (prev_session.year, prev_session.month)


def sessions_from_upstox(start: dt.date, end: dt.date) -> list[dt.date]:
    """Trading days in [start, end] according to Upstox index candles (today via the intraday endpoint)."""
    out: set[dt.date] = set()
    s = start
    while s <= end:
        e = min(s + dt.timedelta(days=364), end)
        r = requests.get(f"https://api.upstox.com/v3/historical-candle/{NIFTY}/days/1/{e}/{s}", timeout=30)
        r.raise_for_status()
        out |= {dt.date.fromisoformat(c[0][:10]) for c in r.json()["data"]["candles"]}
        s = e + dt.timedelta(days=1)
    if end >= dt.date.today():
        try:
            r = requests.get(f"https://api.upstox.com/v3/historical-candle/intraday/{NIFTY}/days/1", timeout=30)
            out |= {dt.date.fromisoformat(c[0][:10]) for c in r.json()["data"]["candles"]}
        except Exception:
            pass
    return sorted(d for d in out if start <= d <= end)
