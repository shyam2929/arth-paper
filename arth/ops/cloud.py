"""
Arth's paper desk run from a fresh cloud session (a Claude scheduled task). Nothing on disk survives between
runs, so each evening the run rebuilds what it needs and carries only the ledger forward, as JSON:

    python -m arth.ops.cloud fetch                      # NSE files for the last 500 days + index candles.
                                                        # Resumable: repeat until the last line is FETCH_OK.
    python -m arth.ops.cloud nightly --ledger L.json --out OUT
        # restore the ledger, process every new session, write OUT/index.html (dashboard, artifact form),
        # OUT/state/ledger.json (the ledger to carry forward) and OUT/summary.txt. Last line RUN_OK / RUN_FAILED.
    python -m arth.ops.cloud seed --out OUT --start 2026-10-01 --capital 1000000     # the first ledger
    python -m arth.ops.cloud bundle --out code.json      # package the code for the artifact
    python -m arth.ops.cloud restore --ledger L.json     # ledger JSON -> data/paper.sqlite (moving to a server)

A 500-calendar-day window (~340 sessions) gives exactly the same targets as the full history (tested on the
24 Sep and 30 Jun 2026 signal dates): the 12-1 score needs 253 sessions and the history filter 260.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, shutil, sqlite3, subprocess, sys, time, traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
RAW = ROOT / "data/raw"
STATUS = ROOT / "data/fetch_status.json"
TABLES = ("meta", "days", "fills", "state", "plans", "events")
RUNTIME = ("arth/", "config/", "README.md")


def ist_today() -> dt.date:
    return dt.datetime.now(IST).date()


# ---- data --------------------------------------------------------------------------------------------
def _valid(p: Path) -> bool:
    try:
        import pyarrow.parquet as pq
        pq.read_metadata(p); return True
    except Exception:
        return False


def fetch(days: int = 500, budget: float = 420.0, today: dt.date | None = None) -> bool:
    import pandas as pd, requests
    from concurrent.futures import ThreadPoolExecutor
    from arth.data import nse_archive as NA, calendar as CAL, indices as IX
    from arth.data.update import _cm_only
    t0 = time.time()
    today = today or ist_today()
    since = today - dt.timedelta(days=days)
    for sub in ("cm", "bc", "mcap"):
        (RAW / sub).mkdir(parents=True, exist_ok=True)
    for p in list((RAW / "cm").glob("*.parquet")) + list((RAW / "bc").glob("*.parquet")):
        if not _valid(p):                                   # a run cut off mid-write leaves a broken file
            p.unlink()
    sessions = CAL.sessions_from_upstox(since, today)
    have = lambda sub, d: (RAW / sub / f"{d:%Y%m%d}.parquet").exists()
    todo = [d for d in sessions if not (have("cm", d) and have("bc", d))]
    sess = [requests.Session() for _ in range(3)]

    def one(i_d):
        i, d = i_d
        if time.time() - t0 > budget:
            return d, "skipped", "skipped"
        s = sess[i % 3]
        cm = "cached" if have("cm", d) else _cm_only(d, RAW, s)["cm"]
        pr = "cached" if have("bc", d) else NA.one_pr(d, RAW, s)["pr"]
        return d, cm, pr

    with ThreadPoolExecutor(3) as ex:
        results = list(ex.map(one, enumerate(todo)))
    idx_path = ROOT / "data/indices.pkl"
    if time.time() - t0 < budget + 120 or not idx_path.exists():
        idx = IX.load_all((since - dt.timedelta(days=30)).isoformat(), today.isoformat())
        try:
            for k, key in IX.KEYS.items():
                d, px = IX.candles_today(key)
                if d is not None:
                    idx.loc[pd.Timestamp(d), k] = px
        except Exception:
            pass
        idx.sort_index().to_pickle(idx_path)
    miss_cm = [d.isoformat() for d in sessions if not have("cm", d)]
    miss_bc = [d.isoformat() for d in sessions if not have("bc", d)]
    st = {"today": today.isoformat(), "since": since.isoformat(), "sessions": [d.isoformat() for d in sessions],
          "missing_cm": miss_cm, "missing_bc": miss_bc, "seconds": round(time.time() - t0),
          "blocked": [(d.isoformat(), c, p) for d, c, p in results if c in ("blocked", "skipped") or p in ("blocked", "skipped")]}
    STATUS.write_text(json.dumps(st, indent=1))
    # only today's files may still be outstanding (NSE publishes them in the evening)
    ok = all(d == today.isoformat() for d in miss_cm + miss_bc) and not [b for b in st["blocked"] if b[0] != today.isoformat()]
    print(f"sessions {len(sessions)}, missing cm {miss_cm[-3:]} bc {miss_bc[-3:]}, {st['seconds']}s")
    print("FETCH_OK" if ok else "FETCH_PARTIAL")
    return ok


def _ready_through(st: dict) -> tuple[str, str]:
    """The last session every file is in for, and a note. Gaps before it are fatal (prices would be wrong)."""
    sessions, miss_cm, miss_bc = st["sessions"], set(st["missing_cm"]), set(st["missing_bc"])
    gaps = [d for d in sessions[:-1] if d in miss_cm]
    if gaps:
        raise RuntimeError(f"NSE price files still missing for {gaps[:5]}{'...' if len(gaps) > 5 else ''}; run fetch again")
    last = sessions[-1] if sessions[-1] not in miss_cm else sessions[-2]
    note = ""
    if last in miss_bc:                                     # corporate-action list not out yet: wait a session
        note = f"NSE corporate-action list for {last} not out yet; {last} is processed on the next run"
        last = sessions[sessions.index(last) - 1]
    elif sessions[-1] in miss_cm:
        note = f"NSE closing prices for {sessions[-1]} not out yet; processed on the next run"
    return last, note


def build_panel(through: str) -> Path:
    from arth.data import panel as PN
    sel = ROOT / "data/sel"
    shutil.rmtree(sel, ignore_errors=True)
    stop = through.replace("-", "")
    for sub in ("cm", "bc"):
        (sel / sub).mkdir(parents=True)
        for p in (RAW / sub).glob("*.parquet"):
            if p.stem[:8] <= stop:
                os.symlink(p.resolve(), sel / sub / p.name)
    out = ROOT / "data/panel_nse.pkl"
    PN.build(str(sel / "cm"), str(out), str(ROOT / "data/indices.pkl"), str(sel / "bc"))
    return out


# ---- ledger <-> JSON ----------------------------------------------------------------------------------
def export_ledger(db: Path, path: Path) -> dict:
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    out = {"format": "arth-ledger-1", "exported": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "tables": {}}
    for t in TABLES:
        if t == "state":                                    # only the latest state is needed to carry on
            q = "SELECT * FROM state ORDER BY date DESC LIMIT 1"
        elif t == "fills":
            q = "SELECT * FROM fills ORDER BY id"
        elif t == "events":
            q = "SELECT * FROM events ORDER BY ts, rowid"
        else:
            q = f"SELECT * FROM {t}"
        out["tables"][t] = [dict(r) for r in con.execute(q).fetchall()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, separators=(",", ":")))
    return out


def restore_ledger(path: Path, db: Path) -> None:
    from arth import paper
    obj = json.loads(Path(path).read_text())
    if obj.get("format") != "arth-ledger-1":
        raise RuntimeError(f"{path} is not an Arth ledger")
    for p in (db, db.with_suffix(".sqlite-wal"), db.with_suffix(".sqlite-shm")):
        if p.exists():
            p.unlink()
    con = paper.connect(db)
    for t in TABLES:
        for r in obj["tables"].get(t, []):
            cols = list(r)
            con.execute(f"INSERT INTO {t}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [r[c] for c in cols])
    con.commit(); con.close()


def _set_meta(db: Path, kv: dict):
    con = sqlite3.connect(db)
    for k, v in kv.items():
        con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, None if v is None else str(v)))
    con.commit(); con.close()


# ---- the evening run -------------------------------------------------------------------------------------
def nightly(ledger: Path, out: Path, full: bool = False, runner: str = "Claude cloud nightly") -> int:
    from arth import paper
    from arth.ops import report
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    db = ROOT / "data/paper.sqlite"
    try:
        st = json.loads(STATUS.read_text())
        through, note = _ready_through(st)
        restore_ledger(ledger, db)
        before = paper.load_state(paper.connect(db))[0]
        panel = build_panel(through)
        res = paper.run(db=db, panel=panel, until=through)
        _set_meta(db, {"data_through": through, "last_run_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                       "runner": runner, "run_status": "ok", "run_note": note,
                       "next_rebalance": res.get("next_rebalance"), "code_run": paper.code_version()})
        if note:
            con = paper.connect(db); paper.log(con, through, "info", note); con.commit(); con.close()
        report.write(db, panel, out_dir=out, fragment=not full)
        new = export_ledger(db, out / "state/ledger.json")
        old = json.loads(Path(ledger).read_text())
        for t in ("days", "fills"):                         # a ledger only ever grows
            if len(new["tables"][t]) < len(old["tables"].get(t, [])):
                raise RuntimeError(f"ledger shrank ({t}: {len(old['tables'][t])} -> {len(new['tables'][t])}); not publishing")
        text = report.summary(db)
        if res["processed"]:
            text += f" Processed {', '.join(res['processed'])}."
        elif before:
            text += " No new session to process."
        if note:
            text += f" Note: {note}."
        (out / "summary.txt").write_text(text + "\n")
        print(text)
        print("RUN_OK " + json.dumps({"through": through, "processed": res["processed"], "last": res["last"],
                                      "next_rebalance": res.get("next_rebalance")}))
        return 0
    except BaseException as e:                              # SystemExit from paper.run included
        msg = f"{type(e).__name__}: {e}"
        for p in (out / "index.html", out / "state/ledger.json"):   # nothing from a failed run can be published
            if p.exists():
                p.unlink()
        (out / "summary.txt").write_text(f"Arth paper run FAILED: {msg}\n")
        traceback.print_exc()
        print("RUN_FAILED " + msg)
        return 1


def seed(out: Path, start: str, capital: float) -> Path:
    from arth import paper
    db = Path(out) / "seed.sqlite"
    if db.exists():
        db.unlink()
    paper.init(start, capital, db=db)
    path = Path(out) / "state/ledger.json"
    export_ledger(db, path)
    for q in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        if q.exists():
            q.unlink()
    return path


def bundle(out: Path) -> dict:
    files = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True).stdout.split()
    files = [f for f in files if f.startswith(RUNTIME) and not f.startswith("arth/research/")]
    version = subprocess.run(["git", "-C", str(ROOT), "describe", "--always", "--tags", "--dirty"],
                             capture_output=True, text=True).stdout.strip()
    obj = {"format": "arth-code-1", "version": version, "files": {f: (ROOT / f).read_text() for f in sorted(files)}}
    Path(out).write_text(json.dumps(obj, separators=(",", ":")))
    print(f"{len(files)} files, {Path(out).stat().st_size / 1024:.0f} KB, version {version}")
    return obj


def main(argv=None):
    ap = argparse.ArgumentParser(prog="arth.ops.cloud")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("fetch"); a.add_argument("--days", type=int, default=500); a.add_argument("--budget", type=float, default=420)
    a = sub.add_parser("nightly"); a.add_argument("--ledger", required=True); a.add_argument("--out", required=True)
    a.add_argument("--full", action="store_true", help="write a complete HTML document (static hosting) instead of an artifact fragment")
    a.add_argument("--runner", default="Claude cloud nightly", help="label shown on the dashboard")
    a = sub.add_parser("seed"); a.add_argument("--out", required=True); a.add_argument("--start", default="2026-10-01")
    a.add_argument("--capital", type=float, default=1_000_000)
    a = sub.add_parser("bundle"); a.add_argument("--out", required=True)
    a = sub.add_parser("restore", help="ledger JSON -> data/paper.sqlite (moving to a server)")
    a.add_argument("--ledger", required=True); a.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        return 0 if fetch(a.days, a.budget) else 3
    if a.cmd == "nightly":
        return nightly(Path(a.ledger), Path(a.out), full=a.full, runner=a.runner)
    if a.cmd == "seed":
        print(seed(Path(a.out), a.start, a.capital)); return 0
    if a.cmd == "restore":
        if (ROOT / "data/paper.sqlite").exists() and not a.force:
            print("data/paper.sqlite already exists; pass --force to replace it"); return 1
        restore_ledger(Path(a.ledger), ROOT / "data/paper.sqlite"); print("restored to", ROOT / "data/paper.sqlite"); return 0
    if a.cmd == "bundle":
        bundle(Path(a.out)); return 0


if __name__ == "__main__":
    sys.exit(main())
