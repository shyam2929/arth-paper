"""
Upstox daily login without a browser (phase 2).

Morning: `python -m arth.ops.token_webhook request` asks Upstox to send you an approval prompt (app/WhatsApp).
When you approve, Upstox POSTs the token to the notifier webhook configured on your API app. This module
runs that receiver: `python -m arth.ops.token_webhook serve` (put it behind HTTPS, e.g. Caddy, at the URL you
register as the notifier). The token is stored with mode 600 and expires at 03:30 IST next day.
"""
from __future__ import annotations
import json, os, sys, datetime as dt
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOKEN_FILE = Path(os.environ.get("ARTH_TOKEN_FILE", ROOT / "data/.upstox_token.json"))


def save_token(payload: dict, path: Path = TOKEN_FILE) -> dict:
    tok = payload.get("access_token")
    if not tok:
        raise ValueError("payload has no access_token")
    rec = {"access_token": tok, "expires_at": payload.get("expires_at"), "issued_at": payload.get("issued_at"),
           "received": dt.datetime.now().isoformat(timespec="seconds")}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec)); os.chmod(tmp, 0o600); tmp.replace(path)
    return rec


def load_token(path: Path = TOKEN_FILE, now: dt.datetime | None = None) -> str | None:
    """The stored token if still valid (Upstox tokens expire at 03:30 IST), else None."""
    if not path.exists():
        return None
    rec = json.loads(path.read_text())
    exp = rec.get("expires_at")
    now = now or dt.datetime.now(dt.timezone.utc)
    if exp:
        try:
            e = float(exp) / (1000 if float(exp) > 1e11 else 1)
            if now.timestamp() >= e:
                return None
        except ValueError:
            pass
    return rec["access_token"]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            save_token(json.loads(self.rfile.read(n) or b"{}"))
            self.send_response(200)
        except Exception:
            self.send_response(400)
        self.end_headers()

    def log_message(self, *a):
        pass


def main(argv=None):
    argv = argv or sys.argv[1:]
    if argv and argv[0] == "request":
        from arth.broker.upstox import Upstox
        print(Upstox.request_token(os.environ["UPSTOX_CLIENT_ID"], os.environ["UPSTOX_CLIENT_SECRET"]))
        return 0
    if argv and argv[0] == "serve":
        HTTPServer(("127.0.0.1", int(os.environ.get("ARTH_WEBHOOK_PORT", 8787))), Handler).serve_forever()
    print(__doc__); return 0


if __name__ == "__main__":
    sys.exit(main())
