"""Cloud runner and overnight repairs: ledger JSON round trip, late corporate actions, symbol changes."""
import json, sqlite3
import pandas as pd
import pytest

from arth import paper
from arth.config import StrategyParams
from arth.ops import cloud, report
from tests.test_paper import _synthetic_panel

PARAMS = StrategyParams(top_n=5, overlay=False, universe_size=30, min_price=0, min_history=260, scorer="121")


def _equity(db):
    return pd.read_sql("select date, equity from days", sqlite3.connect(db), parse_dates=["date"]).set_index("date").equity


def test_indian_grouping():
    assert report._group_in(1_000_000) == "10,00,000"
    assert report._group_in(12_345) == "12,345" and report._group_in(123) == "123"
    assert report._inr(-2_50_00_000) == "-₹2,50,00,000"


def test_ledger_json_round_trip(tmp_path):
    panel, dates = _synthetic_panel(tmp_path)
    db = tmp_path / "a.sqlite"
    paper.init(str(dates[300].date()), 1_000_000, db=db)
    paper.run(db=db, panel=panel, params=PARAMS, until=str(dates[340].date()))
    j1 = cloud.export_ledger(db, tmp_path / "l1.json")
    db2 = tmp_path / "b.sqlite"
    cloud.restore_ledger(tmp_path / "l1.json", db2)
    j2 = cloud.export_ledger(db2, tmp_path / "l2.json")
    assert j1["tables"] == j2["tables"]
    # carrying on from the restored ledger gives the same result as never stopping
    paper.run(db=db, panel=panel, params=PARAMS); paper.run(db=db2, panel=panel, params=PARAMS)
    assert _equity(db).round(4).equals(_equity(db2).round(4))


def test_late_corporate_action_is_applied_once(tmp_path):
    panel, dates = _synthetic_panel(tmp_path)
    P = pd.read_pickle(panel)
    late = dict(P); late["FAC"] = P["FAC"] * 0 + 1.0            # the split is not in the lists yet
    p_late = tmp_path / "late.pkl"; pd.to_pickle(late, p_late)
    db = tmp_path / "p.sqlite"
    paper.init(str(dates[300].date()), 1_000_000, db=db)
    paper.run(db=db, panel=p_late, params=PARAMS, until=str(dates[330].date()))
    eq = _equity(db)
    assert eq.iloc[-1] / eq.iloc[-2] - 1 < -0.10                 # unadjusted: the held split name looks crushed
    paper.run(db=db, panel=panel, params=PARAMS, until=str(dates[332].date()))
    paper.run(db=db, panel=panel, params=PARAMS, until=str(dates[334].date()))
    eq = _equity(db)
    assert abs(eq.loc[dates[331]] / eq.loc[dates[329]] - 1) < 0.08
    ev = pd.read_sql("select msg from events where msg like '%applied late%'", sqlite3.connect(db))
    assert len(ev) == 1 and "S29" in ev.msg.iloc[0]


def test_symbol_change_moves_the_holding(tmp_path):
    panel, dates = _synthetic_panel(tmp_path)
    db = tmp_path / "p.sqlite"
    paper.init(str(dates[300].date()), 1_000_000, db=db)
    paper.run(db=db, panel=panel, params=PARAMS, until=str(dates[340].date()))
    held = paper.load_state(sqlite3.connect(db))[1]["pos"]
    assert "S29" in held
    P = pd.read_pickle(panel)
    ren = {k: (v.rename(columns={"S29": "S29NEW"}) if isinstance(v, pd.DataFrame) and "S29" in v.columns else v) for k, v in P.items()}
    ren["META"] = pd.DataFrame({"symbols": {c: ("S29|S29NEW" if c == "S29NEW" else c) for c in ren["RAW"].columns}})
    p2 = tmp_path / "ren.pkl"; pd.to_pickle(ren, p2)
    paper.run(db=db, panel=p2, params=PARAMS, until=str(dates[345].date()))
    st = paper.load_state(sqlite3.connect(db))[1]
    assert "S29" not in st["pos"] and st["pos"]["S29NEW"] == held["S29"]
    eq = _equity(db)
    assert abs(eq.loc[dates[341]] / eq.loc[dates[340]] - 1) < 0.08


def test_ready_through_waits_for_todays_files():
    st = {"sessions": ["2026-09-28", "2026-09-29", "2026-09-30"], "missing_cm": ["2026-09-30"], "missing_bc": ["2026-09-30"]}
    assert cloud._ready_through(st)[0] == "2026-09-29"
    st = {"sessions": ["2026-09-28", "2026-09-29", "2026-09-30"], "missing_cm": [], "missing_bc": ["2026-09-30"]}
    assert cloud._ready_through(st)[0] == "2026-09-29"            # prices out, corporate actions not yet
    st = {"sessions": ["2026-09-28", "2026-09-29", "2026-09-30"], "missing_cm": [], "missing_bc": []}
    assert cloud._ready_through(st) == ("2026-09-30", "")
    with pytest.raises(RuntimeError):
        cloud._ready_through({"sessions": ["2026-09-28", "2026-09-29", "2026-09-30"], "missing_cm": ["2026-09-28"], "missing_bc": []})
