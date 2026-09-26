"""
NSE daily archive loader (bhavcopy), for building a survivorship-free history.

Two file families, two formats each:
  cash market (CM):  old  content/historical/EQUITIES/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip   (to early Jul 2024)
                     new  content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip                (UDiFF, from Jul 2024)
  derivatives (FO):  old  content/historical/DERIVATIVES/{YYYY}/{MON}/fo{DD}{MON}{YYYY}bhav.csv.zip
                     new  content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip

What is kept (normalised columns, one parquet per day, zip discarded):
  cm/        equity rows (series EQ, BE, BZ): symbol, series, isin, open, high, low, close, prev_close, volume, turnover
  fo_nifty/  NIFTY index options + futures: kind, expiry, strike, opt, open, high, low, close, settle, oi, contracts, lot
  fo_stk/    stock-futures underlyings trading that day (point-in-time F&O list): symbol, expiry, lot

The archive host rate-limits aggressively (sporadic 403). Requests retry with backoff; the job is resumable.
Usage:  python -m arth.data.nse_archive --start 2017-01-01 --end 2026-09-18 --out data/raw --workers 3
"""
from __future__ import annotations
import argparse, io, random, sys, time, zipfile, datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import pandas as pd
import requests

HOST = "https://nsearchives.nseindia.com/"
UDIFF_FROM = dt.date(2024, 7, 8)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://www.nseindia.com/",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
EQ_SERIES = {"EQ", "BE", "BZ"}


def url_old(seg: str, d: dt.date) -> str:
    mon = d.strftime("%b").upper()
    if seg == "CM":
        return f"{HOST}content/historical/EQUITIES/{d.year}/{mon}/cm{d:%d}{mon}{d.year}bhav.csv.zip"
    return f"{HOST}content/historical/DERIVATIVES/{d.year}/{mon}/fo{d:%d}{mon}{d.year}bhav.csv.zip"


def url_new(seg: str, d: dt.date) -> str:
    return f"{HOST}content/{seg.lower()}/BhavCopy_NSE_{seg}_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"


def fetch(session: requests.Session, url: str, tries: int = 7) -> bytes | None:
    """Return zip bytes, None for a genuine miss (404 / no file). Raises after repeated blocks."""
    wait = 2.0
    for i in range(tries):
        try:
            r = session.get(url, headers=HEADERS, timeout=40)
        except requests.RequestException:
            r = None
        if r is not None and r.status_code == 200 and r.content[:2] == b"PK":
            return r.content
        if r is not None and r.status_code == 404:
            return None
        time.sleep(wait + random.random() * wait)
        wait = min(wait * 1.8, 40)
    return b""   # blocked every time: caller records it for a later pass


def _csv(blob: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = z.namelist()[0]
        df = pd.read_csv(z.open(name), low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    return df


def parse_cm(df: pd.DataFrame) -> pd.DataFrame:
    if "TckrSymb" in df.columns:   # UDiFF
        out = pd.DataFrame({
            "symbol": df.TckrSymb, "series": df.SctySrs, "isin": df.ISIN,
            "open": df.OpnPric, "high": df.HghPric, "low": df.LwPric, "close": df.ClsPric,
            "prev_close": df.PrvsClsgPric, "volume": df.TtlTradgVol, "turnover": df.TtlTrfVal})
    else:
        out = pd.DataFrame({
            "symbol": df.SYMBOL, "series": df.SERIES, "isin": df.ISIN,
            "open": df.OPEN, "high": df.HIGH, "low": df.LOW, "close": df.CLOSE,
            "prev_close": df.PREVCLOSE, "volume": df.TOTTRDQTY, "turnover": df.TOTTRDVAL})
    out["series"] = out.series.astype(str).str.strip()
    out["symbol"] = out.symbol.astype(str).str.strip()
    return out[out.series.isin(EQ_SERIES)].reset_index(drop=True)


def parse_fo(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if "TckrSymb" in df.columns:   # UDiFF
        kind = df.FinInstrmTp.map({"IDO": "OPTIDX", "IDF": "FUTIDX", "STO": "OPTSTK", "STF": "FUTSTK"})
        n = pd.DataFrame({
            "kind": kind, "symbol": df.TckrSymb.astype(str).str.strip(), "expiry": pd.to_datetime(df.XpryDt),
            "strike": df.StrkPric, "opt": df.OptnTp, "open": df.OpnPric, "high": df.HghPric, "low": df.LwPric,
            "close": df.ClsPric, "settle": df.SttlmPric, "oi": df.OpnIntrst, "contracts": df.TtlNbOfTxsExctd,
            "volume": df.TtlTradgVol, "lot": df.NewBrdLotQty, "underlying": df.UndrlygPric})
    else:
        n = pd.DataFrame({
            "kind": df.INSTRUMENT.astype(str).str.strip(), "symbol": df.SYMBOL.astype(str).str.strip(),
            "expiry": pd.to_datetime(df.EXPIRY_DT, format="%d-%b-%Y"), "strike": df.STRIKE_PR,
            "opt": df.OPTION_TYP.astype(str).str.strip(), "open": df.OPEN, "high": df.HIGH, "low": df.LOW,
            "close": df.CLOSE, "settle": df.SETTLE_PR, "oi": df.OPEN_INT, "contracts": df.CONTRACTS,
            "volume": pd.NA, "lot": pd.NA, "underlying": pd.NA})
    nifty = n[(n.symbol == "NIFTY") & n.kind.isin(["OPTIDX", "FUTIDX"])].reset_index(drop=True)
    stk = n[n.kind == "FUTSTK"][["symbol", "expiry", "lot", "close", "oi"]].reset_index(drop=True)
    return nifty, stk


def one_day(d: dt.date, out: Path, session: requests.Session) -> dict:
    res = {"date": d.isoformat()}
    cm_path = out / "cm" / f"{d:%Y%m%d}.parquet"
    fo_path = out / "fo_nifty" / f"{d:%Y%m%d}.parquet"
    stk_path = out / "fo_stk" / f"{d:%Y%m%d}.parquet"
    order = (url_old, url_new) if d < UDIFF_FROM else (url_new, url_old)
    if not cm_path.exists():
        blob = None
        for u in order:
            blob = fetch(session, u("CM", d))
            if blob: break
        res["cm"] = "ok" if blob else ("missing" if blob is None else "blocked")
        if blob:
            parse_cm(_csv(blob)).to_parquet(cm_path, index=False)
    else:
        res["cm"] = "cached"
    if not fo_path.exists():
        blob = None
        for u in order:
            blob = fetch(session, u("FO", d))
            if blob: break
        res["fo"] = "ok" if blob else ("missing" if blob is None else "blocked")
        if blob:
            nifty, stk = parse_fo(_csv(blob))
            nifty.to_parquet(fo_path, index=False)
            stk.to_parquet(stk_path, index=False)
    else:
        res["fo"] = "cached"
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default=dt.date.today().isoformat())
    ap.add_argument("--out", default="data/raw")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--dates-file", help="optional CSV with a 'date' column of trading days to fetch")
    a = ap.parse_args(argv)
    out = Path(a.out)
    for sub in ("cm", "fo_nifty", "fo_stk"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    if a.dates_file:
        days = [d.date() for d in pd.to_datetime(pd.read_csv(a.dates_file)["date"])]
    else:
        days = [d.date() for d in pd.bdate_range(a.start, a.end)]
    days = [d for d in days if a.start <= d.isoformat() <= a.end]
    log = open(out / "download_log.csv", "a")
    sess = [requests.Session() for _ in range(a.workers)]
    t0 = time.time(); done = 0
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(one_day, d, out, sess[i % a.workers]): d for i, d in enumerate(days)}
        for f in as_completed(futs):
            r = f.result(); done += 1
            log.write(f"{r['date']},{r.get('cm')},{r.get('fo')}\n"); log.flush()
            if done % 50 == 0:
                print(f"{done}/{len(days)} days, {time.time() - t0:.0f}s", flush=True)
    print("finished", done, "days in", round(time.time() - t0), "s")


# ---- corporate actions + market cap (daily "PR" price-report archive) ------------------------------
def url_pr(d: dt.date) -> str:
    return f"{HOST}archives/equities/bhavcopy/pr/PR{d:%d%m%y}.zip"


def one_pr(d: dt.date, out: Path, session: requests.Session) -> dict:
    """Keep the corporate-action list (Bc*.csv) and, when present, the market-cap file (mcap*.csv)."""
    bc_path = out / "bc" / f"{d:%Y%m%d}.parquet"
    if bc_path.exists():
        return {"date": d.isoformat(), "pr": "cached"}
    blob = fetch(session, url_pr(d))
    if not blob:
        return {"date": d.isoformat(), "pr": "missing" if blob is None else "blocked"}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            low = name.lower()
            if low.startswith("bc") and low.endswith(".csv"):
                try:
                    bc = pd.read_csv(z.open(name), dtype=str, on_bad_lines="skip")
                except pd.errors.EmptyDataError:
                    bc = pd.DataFrame(columns=["SERIES", "SYMBOL", "SECURITY", "RECORD_DT", "BC_STRT_DT",
                                               "BC_END_DT", "EX_DT", "ND_STRT_DT", "ND_END_DT", "PURPOSE"])
                bc.columns = [c.strip() for c in bc.columns]
                bc = bc.apply(lambda c: c.str.strip())
                bc.to_parquet(bc_path, index=False)
            elif low.startswith("mcap") and low.endswith(".csv"):
                try:
                    mc = pd.read_csv(z.open(name), dtype=str, on_bad_lines="skip")
                except pd.errors.EmptyDataError:
                    continue
                mc.columns = [c.strip() for c in mc.columns]
                mc = mc.apply(lambda c: c.str.strip())
                mc.to_parquet(out / "mcap" / f"{d:%Y%m%d}.parquet", index=False)
    return {"date": d.isoformat(), "pr": "ok"}


def main_pr(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-01-01"); ap.add_argument("--end", default=dt.date.today().isoformat())
    ap.add_argument("--out", default="data/raw"); ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--dates-file")
    a = ap.parse_args(argv)
    out = Path(a.out)
    for sub in ("bc", "mcap"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    days = [d.date() for d in (pd.to_datetime(pd.read_csv(a.dates_file)["date"]) if a.dates_file
                               else pd.bdate_range(a.start, a.end))]
    days = [d for d in days if a.start <= d.isoformat() <= a.end]
    sess = [requests.Session() for _ in range(a.workers)]
    log = open(out / "pr_log.csv", "a"); n = 0
    with ThreadPoolExecutor(a.workers) as ex:
        for f in as_completed([ex.submit(one_pr, d, out, sess[i % a.workers]) for i, d in enumerate(days)]):
            r = f.result(); n += 1; log.write(f"{r['date']},{r['pr']}\n"); log.flush()
            if n % 100 == 0:
                print(n, "PR days", flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pr":
        sys.exit(main_pr(sys.argv[2:]))
    sys.exit(main())
