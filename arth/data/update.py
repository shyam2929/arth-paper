"""
Daily data update: fetch the NSE files for every trading day not yet on disk, refresh index history,
rebuild the panel. Safe to run repeatedly; returns the latest trading day now covered.

    python -m arth.data.update                 # incremental
    python -m arth.data.update --since 2024-06-01 --no-fo   # first install on a new server (~5 minutes)
"""
from __future__ import annotations
import argparse, datetime as dt, glob, sys
from pathlib import Path
import pandas as pd
import requests

from arth.data import nse_archive as NA, calendar as CAL, panel as PN, indices as IX

ROOT = Path(__file__).resolve().parents[2]


class DataNotReady(RuntimeError):
    """Today's NSE file is not published yet (normally out by 18:30 IST); retry later."""


def have_days(raw: Path) -> set[dt.date]:
    return {dt.date(int(p.stem[:4]), int(p.stem[4:6]), int(p.stem[6:8])) for p in (raw / "cm").glob("*.parquet")}


def update(raw: Path = ROOT / "data/raw", since: str | None = None, fo: bool = True, today: dt.date | None = None,
           rebuild: bool = True) -> dt.date:
    today = today or dt.date.today()
    for sub in ("cm", "fo_nifty", "fo_stk", "bc", "mcap"):
        (raw / sub).mkdir(parents=True, exist_ok=True)
    have = have_days(raw)
    start = dt.date.fromisoformat(since) if since else (max(have) + dt.timedelta(days=1) if have else dt.date(2017, 1, 1))
    sessions = CAL.sessions_from_upstox(start, today) if start <= today else []
    todo = [d for d in sessions if d not in have]
    from concurrent.futures import ThreadPoolExecutor
    sess = [requests.Session() for _ in range(3)]

    def one(i_d):
        i, d = i_d
        s = sess[i % 3]
        r = NA.one_day(d, raw, s) if fo else _cm_only(d, raw, s)
        NA.one_pr(d, raw, s)
        return d, r.get("cm")

    with ThreadPoolExecutor(3 if len(todo) > 3 else 1) as ex:
        results = list(ex.map(one, enumerate(todo)))
    blocked = sorted((d, why) for d, why in results if why not in ("ok", "cached"))
    if blocked:
        last_ok = max(have_days(raw)) if have_days(raw) else None
        if blocked[-1][0] == today and len(blocked) == 1:
            raise DataNotReady(f"NSE file for {today} not available yet ({blocked[-1][1]})")
        raise RuntimeError(f"could not fetch {[(str(d), why) for d, why in blocked]}; last day on disk {last_ok}")
    # index history (public Upstox candles), incremental
    ip = ROOT / "data/indices.pkl"
    old = pd.read_pickle(ip) if ip.exists() else None
    from_ = (old.index[-1] - pd.Timedelta(days=10)).date().isoformat() if old is not None else "2017-01-01"
    new = IX.load_all(from_, today.isoformat())
    try:
        intraday = {k: IX.candles_today(v) for k, v in IX.KEYS.items()}
        for k, (d, px) in intraday.items():
            if d is not None:
                new.loc[pd.Timestamp(d), k] = px
    except Exception:
        pass
    idx = new if old is None else pd.concat([old[~old.index.isin(new.index)], new]).sort_index()
    idx.to_pickle(ip)
    if rebuild:
        PN.build(str(raw / "cm"), str(ROOT / "data/panel_nse.pkl"), str(ip), str(raw / "bc"))
    return max(have_days(raw))


def _cm_only(d: dt.date, raw: Path, session) -> dict:
    p = raw / "cm" / f"{d:%Y%m%d}.parquet"
    if p.exists():
        return {"cm": "cached"}
    order = (NA.url_old, NA.url_new) if d < NA.UDIFF_FROM else (NA.url_new, NA.url_old)
    blob = None
    for u in order:
        blob = NA.fetch(session, u("CM", d))
        if blob:
            break
    if blob:
        NA.parse_cm(NA._csv(blob)).to_parquet(p, index=False)
        return {"cm": "ok"}
    return {"cm": "missing" if blob is None else "blocked"}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--since"); ap.add_argument("--no-fo", action="store_true")
    a = ap.parse_args(argv)
    try:
        last = update(since=a.since, fo=not a.no_fo)
    except DataNotReady as e:
        print("not ready:", e); return 2
    print("data up to", last)
    return 0


if __name__ == "__main__":
    sys.exit(main())
