"""JSON REST API + web console, standard library only. A background thread runs the
Automator every EOR_TICK_SECONDS so the production line advances without anyone clicking."""
import hmac
import json
import re
import threading
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import rules
from .service import ValidationError

STATIC = Path(__file__).parent / "static"


def _json_default(o):
    if isinstance(o, (Decimal, date)):
        return str(o)
    raise TypeError(type(o).__name__)


def routes(p):
    s = p.store
    lim = lambda q: int(q.get("limit", ["200"])[0])
    filt = lambda q, *keys: {k: q[k][0] for k in keys if k in q}
    return [
        ("GET", r"/api/health", lambda m, q, b: {"ok": True}),
        ("GET", r"/api/dashboard", lambda m, q, b: p.dashboard()),
        ("GET", r"/api/countries", lambda m, q, b: rules.countries()),
        ("POST", r"/api/quote", lambda m, q, b: rules.quote(b["country"], b.get("engagement_type", "EOR").upper(),
                                                           b["salary_annual"])),
        ("GET", r"/api/clients", lambda m, q, b: s.find("clients", limit=lim(q), **filt(q, "status"))),
        ("POST", r"/api/clients", lambda m, q, b: p.create_client(b)),
        ("GET", r"/api/clients/(\w+)", lambda m, q, b: p._get("clients", m[1])),
        ("GET", r"/api/workers", lambda m, q, b: s.find("workers", limit=lim(q), **filt(q, "status", "client_id"))),
        ("POST", r"/api/workers", lambda m, q, b: p.create_worker(b)),
        ("GET", r"/api/workers/(\w+)", lambda m, q, b: p._get("workers", m[1])),
        ("PATCH", r"/api/workers/(\w+)", lambda m, q, b: p.update_worker(m[1], b)),
        ("POST", r"/api/workers/(\w+)/offboard", lambda m, q, b: p.offboard(m[1], b)),
        ("GET", r"/api/documents", lambda m, q, b: s.find("documents", limit=lim(q), **filt(q, "owner_id"))),
        ("GET", r"/api/documents/(\w+)", lambda m, q, b: p._get("documents", m[1])),
        ("GET", r"/api/timesheets", lambda m, q, b: s.find("timesheets", limit=lim(q), **filt(q, "worker_id", "status"))),
        ("POST", r"/api/timesheets", lambda m, q, b: p.submit_timesheet(b)),
        ("GET", r"/api/tasks", lambda m, q, b: s.find("tasks", order="created_at DESC", limit=lim(q),
                                                      **filt(q, "status"))),
        ("POST", r"/api/tasks/(\w+)/resolve", lambda m, q, b: p.resolve_task(m[1], b)),
        ("GET", r"/api/payruns", lambda m, q, b: s.find("pay_runs", order="created_at DESC", limit=lim(q),
                                                        **filt(q, "client_id", "status"))),
        ("GET", r"/api/payruns/(\w+)", lambda m, q, b: p.payrun_detail(m[1])),
        ("GET", r"/api/invoices", lambda m, q, b: s.find("invoices", order="created_at DESC", limit=lim(q),
                                                         **filt(q, "client_id", "status"))),
        ("GET", r"/api/events", lambda m, q, b: s.find("events", order="created_at DESC", limit=lim(q),
                                                       **filt(q, "entity_id"))),
        ("GET", r"/api/outbox", lambda m, q, b: s.find("outbox", order="created_at DESC", limit=lim(q))),
        ("POST", r"/api/automation/tick", lambda m, q, b: p.engine.tick(
            rules.parse_date(b["today"]) if b.get("today") else None)),
        ("POST", r"/api/webhooks/(\w+)", lambda m, q, b: p.webhook(m[1], b)),
    ]


def make_handler(platform):
    table = [(meth, re.compile(pat + "$"), fn) for meth, pat, fn in routes(platform)]
    key = platform.cfg.api_key

    class Handler(BaseHTTPRequestHandler):
        server_version = "LanceSoftEOR/1.0"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, payload, ctype="application/json"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, default=_json_default).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method):
            url = urlparse(self.path)
            if method == "GET" and url.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if key and url.path.startswith("/api/") and url.path != "/api/health":
                if not hmac.compare_digest(self.headers.get("X-API-Key", ""), key):
                    return self._send(401, {"error": "invalid or missing X-API-Key"})
            for meth, pat, fn in table:
                m = pat.match(url.path)
                if m and meth == method:
                    try:
                        length = int(self.headers.get("Content-Length") or 0)
                        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                        with platform.store.lock:
                            return self._send(200, fn(m, parse_qs(url.query), body))
                    except LookupError as e:
                        return self._send(404, {"error": str(e).strip("'")})
                    except (ValidationError, ValueError, KeyError, json.JSONDecodeError) as e:
                        return self._send(400, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

    return Handler


def start_scheduler(platform, stop):
    def loop():
        while not stop.wait(platform.cfg.tick_seconds):
            try:
                platform.engine.tick()
            except Exception as e:  # keep the line running; the failure is in the audit log
                platform.store.audit("system", "scheduler", "tick_failed", error=repr(e))
    t = threading.Thread(target=loop, name="eor-scheduler", daemon=True)
    t.start()
    return t


def serve(platform, scheduler=True):
    httpd = ThreadingHTTPServer((platform.cfg.host, platform.cfg.port), make_handler(platform))
    stop = threading.Event()
    if scheduler:
        start_scheduler(platform, stop)
    return httpd, stop
