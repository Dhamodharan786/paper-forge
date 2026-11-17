"""Local review screen. Binds to 127.0.0.1 only. Start with: python -m paper_forge review"""
from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import review
from .config import Config, load
from .cost import CapExceeded, CostTracker
from .pdf import build_pdf

PAGE = Path(__file__).parent / "static" / "review.html"


class State:
    """One cost tracker for the whole session, so the spending cap covers every click."""

    def __init__(self, cfg: Config, claude_factory=None):
        self.cfg = cfg
        self.tracker = CostTracker(cfg)
        self.lock = threading.Lock()
        self._claude = None
        self._factory = claude_factory

    def claude(self):
        if self._claude is None:
            if self._factory:
                self._claude = self._factory(self.cfg, self.tracker)
            else:
                from .claude import Claude
                self._claude = Claude(self.cfg, self.tracker)
        return self._claude


def make_handler(state: State):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path == "/":
                    return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                if u.path == "/api/drafts":
                    return self._json(200, {"drafts": review.list_drafts(state.cfg),
                                            "capUsd": state.cfg.spend_cap_usd, "spentUsd": round(state.tracker.total, 4)})
                if u.path == "/api/paper":
                    return self._json(200, review.view(state.cfg, q["key"][0]))
                if u.path == "/api/pdf":
                    key = q["key"][0]
                    paper, rep = review.load(state.cfg, key)
                    data = build_pdf(paper, with_answers=q.get("answers", ["1"])[0] != "0",
                                     problems=review.coding_info(state.cfg, paper, rep))
                    return self._send(200, data, "application/pdf",
                                      {"Content-Disposition": f'attachment; filename="{key}-question-paper.pdf"'})
                self._json(404, {"error": "not found"})
            except (KeyError, ValueError, FileNotFoundError) as e:
                self._json(400, {"error": str(e)})

        def do_POST(self):
            if urlparse(self.path).path != "/api/generate":
                return self._json(404, {"error": "not found"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                key, changes = body["key"], body["changes"]
                if not isinstance(changes, dict) or not changes:
                    return self._json(400, {"error": "No changes to apply"})
                with state.lock:
                    out = review.apply_changes(state.cfg, state.claude(), state.tracker, key, changes, log=lambda *_: None)
                out["sessionSpentUsd"] = round(state.tracker.total, 4)
                return self._json(200, out)
            except CapExceeded as e:
                return self._json(402, {"error": str(e)})
            except (KeyError, ValueError, FileNotFoundError) as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:  # missing key, network, API error: show it, do not crash the server
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    return H


def serve(port: int = 8765, open_browser: bool = True, cfg: Config | None = None) -> None:
    state = State(cfg or load())
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    url = f"http://127.0.0.1:{port}/"
    print(f"Review screen at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
