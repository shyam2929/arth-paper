import datetime as dt
import pandas as pd
import pytest

from arth.portfolio.diff import plan_orders
from arth.ledger.fees import charges, CURRENT
from arth.ledger.lots import LotBook, tax_by_year, fy_of
from arth.risk.pretrade import check_order, Quote, Limits, RateGate, relative_breaker, drawdown_action
from arth.exec.orders import ManagedOrder, St, make_tag, resolve_unknown
from arth.strategy.signals import select


# ---- portfolio diff ------------------------------------------------------------------------------
def test_exit_trim_entry_and_band():
    hold = {"A": 100, "B": 100, "C": 10}
    tgt = {"B": 5_000.0, "C": 10_000.0, "D": 10_000.0}
    px = {"A": 100.0, "B": 100.0, "C": 100.0, "D": 50.0}
    o = {(x.symbol, x.side): x for x in plan_orders(hold, tgt, px, band=0.25)}
    assert o[("A", "SELL")].qty == 100 and o[("A", "SELL")].reason == "exit"
    assert o[("B", "SELL")].qty == 50 and o[("B", "SELL")].reason == "trim"
    assert o[("C", "BUY")].qty == 90 and o[("C", "BUY")].reason == "top_up"
    assert o[("D", "BUY")].qty == 200 and o[("D", "BUY")].reason == "entry"


def test_band_suppresses_small_resize():
    o = plan_orders({"A": 95}, {"A": 10_000.0}, {"A": 100.0}, band=0.25)
    assert o == []


def test_sells_come_before_buys():
    o = plan_orders({"A": 10}, {"B": 1000.0}, {"A": 10.0, "B": 10.0})
    assert [x.side for x in o] == ["SELL", "BUY"]


# ---- fees ----------------------------------------------------------------------------------------
def test_fee_components_current_schedule():
    # buy 1 lakh: brokerage 20, STT 100, exch 3.07, sebi 0.1, stamp 15, GST 18% of 23.17
    assert charges("BUY", 100_000, CURRENT) == pytest.approx(20 + 100 + 3.07 + 0.1 + 15 + 4.17, abs=0.02)
    # sell adds the DP charge (Rs 20 + GST) and no stamp duty
    assert charges("SELL", 100_000, CURRENT) == pytest.approx(20 + 100 + 3.07 + 0.1 + 20 + 7.77, abs=0.02)


# ---- tax lots ------------------------------------------------------------------------------------
def test_fifo_and_holding_period():
    b = LotBook()
    b.buy("X", 10, 1000.0, 0.0, 0.0, dt.date(2024, 1, 1))
    b.buy("X", 10, 2000.0, 0.0, 0.0, dt.date(2024, 6, 1))
    g = b.sell("X", 15, 4500.0, 0.0, 0.0, dt.date(2025, 3, 1))     # 300/share
    assert g == pytest.approx(10 * 200 + 5 * 100)
    assert [r.long_term for r in b.realised] == [True, False]
    assert b.position("X") == 5


def test_stt_is_not_deductible():
    b = LotBook()
    b.buy("X", 1, 100.0, 1.0, 0.1, dt.date(2025, 1, 1))            # cost 100.9
    g = b.sell("X", 1, 110.0, 1.0, 0.11, dt.date(2025, 2, 1))       # proceeds 109.11
    assert g == pytest.approx(109.11 - 100.9)


def test_set_off_and_carry_forward():
    b = LotBook()
    b.buy("A", 1, 100_000.0, 0, 0, dt.date(2024, 5, 1)); b.sell("A", 1, 50_000.0, 0, 0, dt.date(2024, 8, 1))  # ST loss 50k
    b.buy("B", 1, 100_000.0, 0, 0, dt.date(2023, 5, 1)); b.sell("B", 1, 300_000.0, 0, 0, dt.date(2024, 9, 1)) # LT gain 200k
    t = tax_by_year(b.realised)
    fy = t[fy_of(dt.date(2024, 9, 1))]
    assert fy["short_term"] == 0 and fy["long_term"] == pytest.approx(150_000)
    assert fy["tax"] == pytest.approx((150_000 - 125_000) * 0.125 * 1.04)


# ---- pre-trade -----------------------------------------------------------------------------------
def _q(ltp=100.0, age=1.0, now=1000.0):
    return dict(quote=Quote(ltp, now - age), now=now)


def test_pretrade_accepts_clean_order():
    assert check_order(symbol="A", side="BUY", qty=100, limit_price=100.3, product="D", target_list={"A"},
                       target_value=10_000, equity=200_000, median_tv20=1e8, limits=Limits(), **_q()) is None


@pytest.mark.parametrize("kw,reason", [
    (dict(product="I"), "product"),
    (dict(symbol="Z"), "target list"),
    (dict(qty=300), "equity"),
    (dict(limit_price=110.0), "collar"),
])
def test_pretrade_rejects(kw, reason):
    base = dict(symbol="A", side="BUY", qty=100, limit_price=100.3, product="D", target_list={"A"},
                target_value=40_000, equity=200_000, median_tv20=1e8, limits=Limits())
    base.update(kw)
    assert reason in check_order(**base, **_q())


def test_stale_quote_and_liquidity():
    base = dict(symbol="A", side="BUY", qty=10, limit_price=100.0, product="D", target_list={"A"},
                target_value=1_000, equity=200_000, limits=Limits())
    assert "stale" in check_order(**base, median_tv20=1e8, **_q(age=120))
    assert "median traded value" in check_order(**base, median_tv20=10_000, **_q())


def test_rate_gate():
    g = RateGate(per_sec=5, per_day=6)
    assert all(g.allow(now=0.1 * i) for i in range(5))
    assert not g.allow(now=0.6)
    assert g.allow(now=1.5)
    assert not g.allow(now=2.0)          # daily cap of 6 reached


def test_breakers():
    assert relative_breaker(93, 100, 99.5, 100)          # Arth -7%, market -0.5% -> freeze
    assert not relative_breaker(93, 100, 94, 100)        # market -6% -> it's the strategy
    assert drawdown_action(-0.25) == "none" and drawdown_action(-0.33) == "review" and drawdown_action(-0.45) == "cut_to_half"


# ---- orders --------------------------------------------------------------------------------------
def test_tag_and_state_machine():
    t = make_tag("202610", "SELL", "M&M", 1)
    assert t == "arth-202610-S-MM-1" and len(t) <= 40
    o = ManagedOrder(t, "M&M", "SELL", 10, 100.0)
    o.to(St.SENT); o.to(St.ACK); o.on_fill(4, 100.0); o.on_fill(6, 101.0)
    assert o.state == St.FILLED and o.avg_price == pytest.approx(100.6)
    with pytest.raises(ValueError):
        o.to(St.CANCELLED)


def test_timeout_resolution_never_double_sends():
    o = ManagedOrder("arth-202610-B-X-1", "X", "BUY", 10, 50.0); o.to(St.SENT); o.to(St.UNKNOWN)
    resolve_unknown(o, [{"tag": "arth-202610-B-X-1", "order_id": "9", "status": "open", "filled_quantity": 0}])
    assert o.state == St.ACK and o.broker_id == "9"
    o2 = ManagedOrder("arth-202610-B-Y-1", "Y", "BUY", 10, 50.0); o2.to(St.SENT); o2.to(St.UNKNOWN)
    resolve_unknown(o2, [])
    assert o2.state == St.NEW


# ---- selection -----------------------------------------------------------------------------------
def test_buffer_keeps_holdings_within_2n():
    ranked = pd.Series(range(1, 41), index=[f"S{i}" for i in range(1, 41)], dtype=float)
    chosen = select(ranked, holdings=["S25", "S35"], top_n=15, buffer_mult=2.0)
    assert "S25" in chosen and "S35" not in chosen and len(chosen) == 15
