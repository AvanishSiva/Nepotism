"""Local terminal-style portal: http://127.0.0.1:8765 (localhost only, stdlib HTTP server).

POST /api/cmd   {"line": "..."}           -> {"lines": [...], "client": {...}}
GET  /api/task/<id>?from=N                 -> {"lines": [...], "next": N, "done": bool, "rc": int}

Every API call needs the X-Token header (a random token baked into the page), so other
websites open in the browser can't drive the portal. Only the fixed pipeline stages can be
run; there is no arbitrary shell.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from job_agent.db import DEFAULT_DB_PATH, PROJECT_ROOT, connect
from job_agent.portal.commands import STAGES, execute

log = logging.getLogger(__name__)
INDEX = Path(__file__).with_name("index.html")
TOKEN = secrets.token_urlsafe(24)
TASKS: dict[str, dict] = {}
_LOCK = threading.Lock()


def start_task(stage: str) -> str:
    assert stage in STAGES
    with _LOCK:
        for t in TASKS.values():
            if t["stage"] == stage and not t["done"]:
                return t["id"]                       # already running: attach to it
    tid = uuid.uuid4().hex[:8]
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([sys.executable, "-m", "job_agent", stage], cwd=PROJECT_ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    task = {"id": tid, "stage": stage, "lines": [], "done": False, "rc": None}
    TASKS[tid] = task

    def pump():
        for ln in proc.stdout:
            task["lines"].append(ln.rstrip("\n"))
        task["rc"] = proc.wait()
        task["done"] = True
    threading.Thread(target=pump, daemon=True).start()
    return tid


class Handler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB_PATH

    def log_message(self, fmt, *args):            # quiet
        log.debug(fmt, *args)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        if self.headers.get("X-Token") != TOKEN:
            self._json({"error": "forbidden"}, 403)
            return False
        return True

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = INDEX.read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/task/"):
            if not self._authorised():
                return
            tid, _, q = self.path[len("/api/task/"):].partition("?from=")
            t = TASKS.get(tid)
            if not t:
                return self._json({"error": "no such task"}, 404)
            start = int(q or 0)
            self._json({"lines": t["lines"][start:], "next": len(t["lines"]), "done": t["done"], "rc": t["rc"]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/api/cmd" or not self._authorised():
            if self.path != "/api/cmd":
                self._json({"error": "not found"}, 404)
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            line = json.loads(self.rfile.read(n) or b"{}").get("line", "")[:500]
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        conn = connect(self.db_path)
        try:
            out = execute(conn, line)
        finally:
            conn.close()
        if "run" in out.client:
            out.client["task"] = start_task(out.client.pop("run"))
        self._json({"lines": out.lines, "client": out.client})


def serve(port: int = 8765, open_browser: bool = True, db_path=DEFAULT_DB_PATH) -> None:
    Handler.db_path = db_path
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"nepotism portal on {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
