"""Shared fixtures: a fake System One server on localhost and keyword engines."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

import pytest

import exregex as ex
from exregex.testing import referenced_text, resolve


class FakeServer:
    """Answers /v1/systemone requests with a handler; records every request; can fail on cue."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.failures: list[tuple[int, Optional[str], str]] = []  # (status, retry-after, body) served first
        self.handler: Callable[[dict], dict] = keyword_answers({})
        self.reply_override: Optional[Callable[[dict, dict], Any]] = None
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:
                n = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.requests.append(body)
                outer.headers.append(dict(self.headers))
                if outer.failures:
                    status, retry_after, text = outer.failures.pop(0)
                    self.send_response(status)
                    if retry_after is not None:
                        self.send_header("retry-after", retry_after)
                    self.end_headers()
                    self.wfile.write(text.encode())
                    return
                answers = outer.handler(body) if "state" in body else {}
                reply: Any = {"model": body.get("model"), "answers": answers, "usage": {"input_tokens": 100, "output_tokens": 0}}
                if outer.reply_override:
                    reply = outer.reply_override(body, reply)
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def keyword_answers(keywords: dict[str, list[str]]) -> Callable[[dict], dict]:
    """A System One handler: nouls by keyword lookup in the referenced span, choices to the first hit."""

    def hit(instr: str, span: str) -> bool:
        q = instr.lower()
        for trigger, words in keywords.items():
            if trigger in q:
                return any(w in span.lower() for w in words)
        return False

    def handle(body: dict) -> dict:
        state = body["state"]
        out = {}
        for name, q in body["questions"].items():
            instr = q["instructions"] if isinstance(q["instructions"], str) else json.dumps(q["instructions"])
            if q["type"] == "noul":
                if "any segment in `segments`" in instr:
                    found = any(hit(instr, str(v)) for v in (resolve(state, "segments") or {}).values())
                else:
                    found = hit(instr, referenced_text(state, instr))
                out[name] = {"type": "noul", "noul": 0.97 if found else 0.02}
            elif q["type"] == "choice":
                opts = list(q["criteria"])
                segs = resolve(state, "segments") if "`segments`" in instr else None
                pick = opts[-1]
                for o in opts:
                    target = str(segs.get(o, "")) if isinstance(segs, dict) else o
                    if hit(instr, target):
                        pick = o
                        break
                out[name] = {
                    "type": "choice",
                    "choice": pick,
                    "probabilities": {o: (0.9 if o == pick else 0.1 / (len(opts) - 1)) for o in opts},
                    "confidence": 0.88,
                }
            else:
                n = len(q["criteria"])
                out[name] = {
                    "type": "score",
                    "score": n - 1,
                    "legend": {str(i): c for i, c in enumerate(q["criteria"])},
                    "probabilities": {str(i): (1.0 if i == n - 1 else 0.0) for i in range(n)},
                    "confidence": 0.9,
                }
        return out

    return handle


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never leave the machine. Any connection to a host other than localhost fails loudly,
    so a stray .env above the test directory can never turn a test into a billed request."""
    import os
    import socket

    real = socket.getaddrinfo

    def local_only(host, *args, **kwargs):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise OSError(f"tests may not reach {host!r}")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", local_only)
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)  # keys a test loaded from a .env do not leak into the next test


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("EXREGEX_BACKEND", "EXREGEX_MODEL", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "TYPESAFE_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    ex.set_engine(None)
    yield
    ex.set_engine(None)


def no_sleep(_: float) -> None:
    pass
