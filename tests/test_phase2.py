"""Phase-2 pieces testable without the server: token store, reconciliation, Upstox adapter guards."""
import datetime as dt, json
import pytest

from arth.ops import token_webhook as TW
from arth.exec import reconcile as RC
from arth.broker.upstox import Upstox, UpstoxError


def test_token_store_and_expiry(tmp_path):
    p = tmp_path / "tok.json"
    exp = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).timestamp() * 1000
    TW.save_token({"access_token": "abc", "expires_at": exp}, p)
    assert (p.stat().st_mode & 0o777) == 0o600
    assert TW.load_token(p) == "abc"
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)
    assert TW.load_token(p, now=later) is None
    with pytest.raises(ValueError):
        TW.save_token({"nope": 1}, p)


def test_reconcile_counts_t1_and_flags_mismatch():
    hold = [{"tradingsymbol": "DIXON", "quantity": 3, "t1_quantity": 1}, {"tradingsymbol": "ITC", "quantity": 50}]
    b = RC.broker_positions(hold)
    assert b == {"DIXON": 4, "ITC": 50}
    assert RC.diff({"DIXON": 4, "CUPID": 10}, b, ignore={"ITC"}) == {"CUPID": (10, 0)}


class FakeUpstox(Upstox):
    def __init__(self, book):
        super().__init__(token="t", dry_run=False); self._book = book; self.sent = []
    def order_book(self):
        return self._book
    def _call(self, method, url, **kw):
        self.sent.append((method, url, kw)); return {"order_ids": ["1"]}


def test_adapter_refuses_non_delivery_and_duplicate_tags():
    u = FakeUpstox(book=[{"tag": "arth-202610-B-X-1"}])
    with pytest.raises(UpstoxError):
        u.place(instrument_key="NSE_EQ|X", side="BUY", qty=1, limit=10, tag="arth-202610-B-X-1")
    with pytest.raises(UpstoxError):
        u.place(instrument_key="NSE_EQ|X", side="BUY", qty=1, limit=10, tag="t2", product="I")
    oid = u.place(instrument_key="NSE_EQ|X", side="BUY", qty=1, limit=10.123, tag="arth-202610-B-X-2")
    body = u.sent[-1][2]["json"]
    assert oid == "1" and body["order_type"] == "LIMIT" and body["product"] == "D" and body["price"] == 10.12
