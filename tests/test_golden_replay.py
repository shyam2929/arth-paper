"""Golden replay: the production pipeline must reproduce the research backtest within 0.1 pt of CAGR."""
import os, sys, pytest
import pandas as pd

PANEL = os.environ.get("ARTH_GOLDEN_PANEL", "/home/claude/bt/panel_all.pkl")
pytestmark = pytest.mark.skipif(not os.path.exists(PANEL), reason="research panel not available")
os.environ.setdefault("ARTH_PANEL", PANEL)


def test_matches_research_engine():
    from arth.config import StrategyParams
    from arth.strategy.signals import Features
    from arth import backtest as B
    from arth.ledger.fees import LEGACY
    import arth.research.engine_research as R
    P = pd.read_pickle(PANEL)
    stocks = [c for c in P["C"].columns if not c.startswith("ETF_")]
    f = Features.build(P["C"][stocks], P["V"][stocks], P["IDX"]["Nifty_500"])
    res = B.run(f, StrategyParams(top_n=15, overlay=True), schedule=LEGACY)
    ref = R.run(N=15, scorer="ens", overlay="trend")["equity"]
    assert abs(B.cagr(res.equity) - B.cagr(ref)) < 0.001
    assert (res.equity / ref - 1).abs().max() < 0.005
