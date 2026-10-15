"""A sandbox bank: a payments API and a live dashboard, on http://localhost:8077.

    python bank.py            # start it, then open http://localhost:8077
"""
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from erp import VENDORS

PORT = 8077
OPENING = 2_840_000.00
KNOWN = {v[2] for v in VENDORS}          # beneficiaries this company has paid before
_lock = threading.Lock()
_payments = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/payments"):
            with _lock:
                paid = sum(p["amount"] for p in _payments)
                return self._send(200, {"payments": _payments, "balance": OPENING - paid, "opening": OPENING})
        return self._send(200, Path(__file__).with_name("bank.html").read_bytes(), "text/html; charset=utf-8")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/reset":
            with _lock:
                _payments.clear()
            return self._send(200, {"ok": True})
        if self.path == "/api/payments":
            p = {
                "id": f"TX-{90410 + len(_payments)}",
                "time": time.strftime("%H:%M:%S"),
                "beneficiary": body["beneficiary"],
                "account": str(body["account"]),
                "routing": str(body.get("routing", "")),
                "amount": float(body["amount"]),
                "reference": body.get("reference", ""),
                "new_beneficiary": str(body["account"]) not in KNOWN,
                "status": "executed",
            }
            with _lock:
                _payments.append(p)
            return self._send(201, p)
        return self._send(404, {"error": "not found"})


class Bank:
    """The client an agent uses. pay() moves money: there is no undo."""

    def __init__(self, url=f"http://localhost:{PORT}"):
        self.url = url

    def pay(self, beneficiary, account, routing, amount, reference):
        body = json.dumps({"beneficiary": beneficiary, "account": account, "routing": routing,
                           "amount": amount, "reference": reference}).encode()
        req = urllib.request.Request(f"{self.url}/api/payments", body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    def reset(self):
        urllib.request.urlopen(urllib.request.Request(f"{self.url}/api/reset", b"{}", method="POST"), timeout=10)


if __name__ == "__main__":
    print(f"Sandbox bank on http://localhost:{PORT}")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
