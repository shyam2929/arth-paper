"""Index history from Upstox's public historical-candle API (no login needed)."""
from __future__ import annotations
import datetime as dt
import urllib.parse
import pandas as pd
import requests

KEYS = {
    "Nifty_500": "NSE_INDEX|Nifty 500",
    "Nifty_50": "NSE_INDEX|Nifty 50",
    "India_VIX": "NSE_INDEX|India VIX",
    "NiftyM150Momntm50": "NSE_INDEX|NiftyM150Momntm50",
    "Nifty200Momentm30": "NSE_INDEX|Nifty200Momentm30",
    "NIFTY_MIDCAP_150": "NSE_INDEX|NIFTY MIDCAP 150",
}


def candles(key: str, start: str, end: str) -> pd.Series:
    out = []
    s = pd.Timestamp(start)
    while s <= pd.Timestamp(end):
        e = min(s + pd.DateOffset(years=1) - pd.Timedelta(days=1), pd.Timestamp(end))
        url = (f"https://api.upstox.com/v3/historical-candle/{urllib.parse.quote(key, safe='')}"
               f"/days/1/{e:%Y-%m-%d}/{s:%Y-%m-%d}")
        r = requests.get(url, timeout=30, headers={"Accept": "application/json"})
        r.raise_for_status()
        for c in r.json().get("data", {}).get("candles", []):
            out.append((pd.Timestamp(c[0][:10]), float(c[4])))
        s = e + pd.Timedelta(days=1)
    return pd.Series(dict(out)).sort_index()


def load_all(start="2017-01-01", end=None) -> pd.DataFrame:
    end = end or dt.date.today().isoformat()
    return pd.DataFrame({name: candles(k, start, end) for name, k in KEYS.items()})


def candles_today(key: str):
    """Today's close-so-far from the intraday endpoint: (date, price) or (None, None)."""
    url = f"https://api.upstox.com/v3/historical-candle/intraday/{urllib.parse.quote(key, safe='')}/days/1"
    r = requests.get(url, timeout=30, headers={"Accept": "application/json"})
    c = r.json().get("data", {}).get("candles", [])
    if not c:
        return None, None
    return pd.Timestamp(c[0][0][:10]).date(), float(c[0][4])
