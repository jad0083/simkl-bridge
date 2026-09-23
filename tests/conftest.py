"""A scripted stand-in for api.simkl.com.

Every test talks to the real client code through `FakeSimkl`, which records
each request so tests can assert on what was sent (headers, query, body) and
not only on what came back.
"""
import json
import pathlib
import sys
import urllib.parse

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from simkl_bridge.http import Response


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now
        self.slept = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept += seconds
        self.now += seconds


class FakeSimkl:
    """Routes are `(method, path) -> callable(request) -> Response | (status, body)`."""

    def __init__(self):
        self.routes = {}
        self.requests = []

    def on(self, method, path, handler):
        self.routes[(method, path)] = handler
        return self

    def json(self, method, path, body, status=200):
        return self.on(method, path, lambda _req: (status, body))

    def __call__(self, method, url, headers=None, data=None, timeout=None):
        parts = urllib.parse.urlsplit(url)
        req = {
            "method": method,
            "url": url,
            "path": parts.path,
            "query": dict(urllib.parse.parse_qsl(parts.query)),
            "headers": dict(headers or {}),
            "form": dict(urllib.parse.parse_qsl(data.decode())) if data else {},
            "body": data.decode() if data else None,
        }
        self.requests.append(req)
        handler = self.routes.get((method, parts.path))
        if handler is None:
            return Response(404, {}, json.dumps({"error": "not_found"}).encode())
        out = handler(req)
        if isinstance(out, Response):
            return out
        status, body = out
        return Response(status, {"Content-Type": "application/json"}, json.dumps(body).encode())

    def calls(self, path):
        return [r for r in self.requests if r["path"] == path]


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake():
    f = FakeSimkl()
    f.json("POST", "/oauth2/token", {
        "access_token": "simkl_at_ONE", "token_type": "Bearer",
        "expires_in": 604800, "refresh_token": "simkl_rt_R", "scope": "media:read",
    })
    return f
