"""
Upstox adapter: the only module with network access to the broker.

Hard rules enforced here, not just in config:
  - product "D" (delivery) only; LIMIT orders only (plain market orders are banned in code)
  - every order carries a tag; `place` refuses a tag already present in today's order book
  - orders go out only from a host whose IP is registered (Upstox rejects others since 1 Apr 2026)

Endpoints: v3 place/modify/cancel on api-hft.upstox.com; v2 order book, holdings, funds and static-IP APIs;
v3 LTP quotes; v3 Access Token Request (the user approves in the app or on WhatsApp; the token arrives on
the notifier webhook). Untested against the live API until phase 2's sandbox suite runs.
"""
from __future__ import annotations
import os
import requests

HFT = "https://api-hft.upstox.com"
API = "https://api.upstox.com"


class UpstoxError(RuntimeError):
    pass


class Upstox:
    def __init__(self, token: str | None = None, dry_run: bool = True, timeout: float = 10.0):
        self.token = token or os.environ.get("UPSTOX_ACCESS_TOKEN", "")
        self.dry_run = dry_run
        self.timeout = timeout
        self.s = requests.Session()

    # ---- plumbing -------------------------------------------------------------------------------
    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                "Content-Type": "application/json"}

    def _call(self, method: str, url: str, **kw) -> dict:
        r = self.s.request(method, url, headers=self._h(), timeout=self.timeout, **kw)
        try:
            body = r.json()
        except ValueError:
            raise UpstoxError(f"{r.status_code} non-JSON from {url}")
        if r.status_code >= 400 or body.get("status") == "error":
            raise UpstoxError(f"{r.status_code} {body.get('errors') or body}")
        return body.get("data", body)

    # ---- reads ----------------------------------------------------------------------------------
    def order_book(self) -> list[dict]:
        return self._call("GET", f"{API}/v2/order/retrieve-all") or []

    def holdings(self) -> list[dict]:
        return self._call("GET", f"{API}/v2/portfolio/long-term-holdings") or []

    def funds(self) -> dict:
        return self._call("GET", f"{API}/v2/user/get-funds-and-margin", params={"segment": "SEC"})

    def ltp(self, instrument_keys: list[str]) -> dict:
        return self._call("GET", f"{API}/v3/market-quote/ltp", params={"instrument_key": ",".join(instrument_keys)})

    def static_ips(self) -> dict:
        return self._call("GET", f"{API}/v2/user/ip")

    # ---- writes ---------------------------------------------------------------------------------
    def place(self, *, instrument_key: str, side: str, qty: int, limit: float, tag: str,
              product: str = "D") -> str:
        if product != "D":
            raise UpstoxError("Arth trades delivery only")
        if not tag or len(tag) > 40:
            raise UpstoxError("tag required, max 40 characters")
        if any(o.get("tag") == tag for o in self.order_book()):
            raise UpstoxError(f"tag {tag} already in today's order book; resolve it instead of resending")
        body = {"quantity": int(qty), "product": "D", "validity": "DAY", "price": round(float(limit), 2),
                "tag": tag, "instrument_token": instrument_key, "order_type": "LIMIT",
                "transaction_type": side.upper(), "disclosed_quantity": 0, "trigger_price": 0,
                "is_amo": False, "slice": False}
        if self.dry_run:
            return f"DRY-{tag}"
        data = self._call("POST", f"{HFT}/v3/order/place", json=body)
        ids = data.get("order_ids") or [data.get("order_id")]
        return ids[0]

    def modify(self, order_id: str, qty: int, limit: float) -> str:
        body = {"order_id": order_id, "quantity": int(qty), "validity": "DAY", "price": round(float(limit), 2),
                "order_type": "LIMIT", "trigger_price": 0}
        if self.dry_run:
            return order_id
        return self._call("PUT", f"{HFT}/v3/order/modify", json=body).get("order_id", order_id)

    def cancel(self, order_id: str) -> None:
        if not self.dry_run:
            self._call("DELETE", f"{HFT}/v3/order/cancel", params={"order_id": order_id})

    # ---- daily login ----------------------------------------------------------------------------
    @staticmethod
    def request_token(client_id: str, client_secret: str) -> dict:
        """Ask Upstox to push an approval prompt to the user; the token lands on the notifier webhook."""
        r = requests.post(f"{API}/v3/login/auth/token/request/{client_id}",
                          json={"client_secret": client_secret}, timeout=10)
        r.raise_for_status()
        return r.json()
