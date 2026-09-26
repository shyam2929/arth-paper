"""
Order state machine with idempotent tags.

Every order carries a deterministic tag (<= 40 chars, Upstox's limit), e.g. arth-202610-S-DIXON-1.
Before any retry the executor looks the tag up in the broker's order book, so a timeout can never turn
into a second order. Allowed transitions are explicit; anything else raises.
"""
from __future__ import annotations
import enum
import re
from dataclasses import dataclass, field


class St(str, enum.Enum):
    NEW = "new"; SENT = "sent"; ACK = "acknowledged"; PARTIAL = "partial"
    FILLED = "filled"; CANCELLED = "cancelled"; REJECTED = "rejected"; UNKNOWN = "unknown"


TERMINAL = {St.FILLED, St.CANCELLED, St.REJECTED}
ALLOWED = {
    St.NEW: {St.SENT, St.REJECTED},
    St.SENT: {St.ACK, St.PARTIAL, St.FILLED, St.REJECTED, St.UNKNOWN, St.CANCELLED},
    St.UNKNOWN: {St.ACK, St.PARTIAL, St.FILLED, St.REJECTED, St.CANCELLED, St.NEW},
    St.ACK: {St.PARTIAL, St.FILLED, St.CANCELLED, St.REJECTED},
    St.PARTIAL: {St.PARTIAL, St.FILLED, St.CANCELLED},
}


def make_tag(month: str, side: str, symbol: str, seq: int) -> str:
    sym = re.sub(r"[^A-Z0-9]", "", symbol.upper())[:20]
    tag = f"arth-{month}-{side[0]}-{sym}-{seq}"
    assert len(tag) <= 40
    return tag


@dataclass
class ManagedOrder:
    tag: str
    symbol: str
    side: str
    qty: int
    limit: float
    state: St = St.NEW
    filled: int = 0
    avg_price: float = 0.0
    broker_id: str | None = None
    reprices: int = 0
    history: list = field(default_factory=list)

    def to(self, new: St, note: str = ""):
        if self.state in TERMINAL:
            raise ValueError(f"{self.tag}: already {self.state.value}")
        if new not in ALLOWED.get(self.state, set()):
            raise ValueError(f"{self.tag}: {self.state.value} -> {new.value} not allowed")
        self.history.append((self.state.value, new.value, note)); self.state = new

    def on_fill(self, qty: int, price: float):
        tot = self.filled + qty
        if tot > self.qty:
            raise ValueError(f"{self.tag}: overfill {tot} > {self.qty}")
        self.avg_price = (self.avg_price * self.filled + price * qty) / tot
        self.filled = tot
        self.to(St.FILLED if tot == self.qty else St.PARTIAL, f"fill {qty}@{price}")


def resolve_unknown(order: ManagedOrder, book: list[dict]) -> ManagedOrder:
    """After a timeout: find the order by tag in the broker book. Absent => safe to resend (state NEW)."""
    hit = [b for b in book if b.get("tag") == order.tag]
    if not hit:
        order.to(St.NEW, "not in book; safe to resend")
        return order
    b = hit[-1]
    order.broker_id = b.get("order_id")
    status = str(b.get("status", "")).lower()
    filled = int(b.get("filled_quantity", 0) or 0)
    if status in ("rejected",):
        order.to(St.REJECTED, b.get("status_message", ""))
    elif status in ("cancelled",):
        order.to(St.CANCELLED)
    elif filled >= order.qty or status == "complete":
        order.to(St.FILLED); order.filled = order.qty; order.avg_price = float(b.get("average_price") or order.limit)
    elif filled > 0:
        order.to(St.PARTIAL); order.filled = filled
    else:
        order.to(St.ACK)
    return order
