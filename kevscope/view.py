"""Open a converted trace in ui.perfetto.dev without uploading it anywhere.

Perfetto's UI can load a trace from http://127.0.0.1:9001 (the address its own
`open_trace_in_ui` helper uses, and the only local origin its content security
policy allows). This serves the one file on that address, tells the browser to
open the UI pointed at it, and stops after the UI has fetched it. The trace
goes from this process to the browser tab on the same machine and nowhere else.
"""
from __future__ import annotations

import http.server
import os
import socketserver
import time
import webbrowser

ORIGIN = "https://ui.perfetto.dev"
PORT = 9001


def ui_url(name: str) -> str:
    return "%s/#!/?url=http://127.0.0.1:%d/%s" % (ORIGIN, PORT, name)


def serve_once(path: str, open_browser: bool = True, timeout: float = 120.0) -> bool:
    """Serve `path` until the UI has fetched it once. Returns True if it did."""
    name = os.path.basename(path)
    with open(path, "rb") as f:
        payload = f.read()
    state = {"served": False}

    class Handler(http.server.BaseHTTPRequestHandler):
        # Browsers open spare connections and send nothing on them; without a
        # timeout (and without threads) one of those would block the real request.
        timeout = 10

        def _headers(self, code: int, length: int = 0) -> None:
            self.send_response(code)
            self.send_header("Access-Control-Allow-Origin", ORIGIN)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(length))
            self.end_headers()

        def do_OPTIONS(self) -> None:      # noqa: N802 (http.server naming)
            self._headers(204)

        def do_GET(self) -> None:          # noqa: N802
            if self.path.lstrip("/") != name:
                self._headers(404)
                return
            self._headers(200, len(payload))
            self.wfile.write(payload)
            state["served"] = True

        def log_message(self, fmt: str, *args) -> None:
            pass

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server(("127.0.0.1", PORT), Handler) as httpd:
        httpd.timeout = 1.0
        url = ui_url(name)
        print("Serving %s on 127.0.0.1:%d for %s" % (name, PORT, ORIGIN))
        print("If no tab opens, open this yourself:\n  %s" % url)
        if open_browser:
            webbrowser.open(url)
        waited = 0.0
        while not state["served"] and waited < timeout:
            httpd.handle_request()          # returns after 1 s if nobody connected
            waited += httpd.timeout
        if state["served"]:
            time.sleep(1.0)                 # let the response thread finish sending
    return bool(state["served"])
