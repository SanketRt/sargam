"""
The local review UI: http://localhost:7000

A standard-library transport over the handlers in api.py. No framework, no
build step, no dependencies -- the local tool should not need a web stack to
show you a page. The hosted server (server.py) speaks to the same handlers.

One process, one store, one lock, because it is a single-user tool on
localhost. The hosted case resolves a store per request instead; that is the
only difference between the two, and it lives in the adapters rather than in
the product.
"""

from __future__ import annotations

import json
import pathlib
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import api
import store as S

_lock = threading.Lock()
_ctx: api.Ctx | None = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, api.PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            with _lock:
                data = api.snapshot(_ctx)
            self._send(200, api.dumps(data), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        fn = api.ROUTES.get(self.path)
        if fn is None:
            self._send(404, b"not found", "text/plain")
            return
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            with _lock:
                out = fn(_ctx, body)
        except Exception as exc:
            out = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        self._send(200, api.dumps(out), "application/json")


def serve(db_path, manuscript_dir, port: int = 7000,
          open_browser: bool = True) -> None:
    global _ctx
    _ctx = api.Ctx(store=S.Store(db_path),
                   manuscript=pathlib.Path(manuscript_dir))
    url = f"http://localhost:{port}"
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"sargam review UI on {url}   (ctrl-c to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()
        _ctx.store.close()
