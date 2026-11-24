"""Review board: the digest page with working ✓ applied / ✗ discard buttons.

A digest file opened from disk can't write to the database, so `python -m jobpilot board`
serves the same page from a tiny local server (127.0.0.1 only) that saves every click.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import digest
from .config import Settings
from .db import now

log = logging.getLogger(__name__)
STATUSES = {"applied", "skipped", "queued"}


def mark(db, jid: str, status: str) -> bool:
    """Set one job's status by its full id. applied_at is kept only while the job is 'applied'."""
    if status not in STATUSES:
        raise ValueError(status)
    if not db.conn.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone():
        return False
    db.set(jid, status=status, applied_at=now() if status == "applied" else None)
    return True


class _Server(HTTPServer):
    # On Windows SO_REUSEADDR lets two servers share a port; keep it off there so a busy port is detected.
    allow_reuse_address = os.name != "nt"


def make_server(settings: Settings, db, port: int = 8765) -> HTTPServer:
    token = secrets.token_urlsafe(16)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body, ctype: str = "application/json"):
            data = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _local_host(self) -> bool:
            # Rejects pages on other sites that point a hostname at 127.0.0.1 (DNS rebinding).
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost")

        def do_GET(self):
            if not self._local_host():
                return self._send(403, {"error": "forbidden"})
            if self.path.split("?")[0] in ("/", "/index.html"):
                return self._send(200, digest.build_html(settings, db, token=token), "text/html")
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/api/mark":
                return self._send(404, {"error": "not found"})
            if not (self._local_host() and secrets.compare_digest(self.headers.get("X-JobPilot-Token", ""), token)):
                return self._send(403, {"error": "reload the page"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                jid, status = str(body["id"]), body["status"]
            except (ValueError, KeyError, TypeError):
                return self._send(400, {"error": "bad request"})
            if status not in STATUSES:
                return self._send(400, {"error": f"unknown status {status!r}"})
            if not mark(db, jid, status):
                return self._send(404, {"error": "job not found"})
            log.info("%-8s %s", status, jid)
            self._send(200, {"ok": True, "counts": digest.stats(db)})

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    try:
        srv = _Server(("127.0.0.1", port), Handler)
    except OSError:
        srv = _Server(("127.0.0.1", 0), Handler)   # port busy: let the OS pick a free one
    srv.token = token
    return srv


def serve(settings: Settings, db, port: int = 8765, open_browser: bool = True):
    srv = make_server(settings, db, port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"JobPilot board: {url}\nClicks save straight to the database. Leave this window open; Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        print(f"Stopped. digest updated: {digest.write_html(settings, db, dated=False)}")
