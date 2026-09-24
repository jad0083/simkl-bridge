"""A stand-in for api.simkl.com, for end-to-end and soak testing.

Implements the subset of Simkl's API the bridge uses, with Simkl's documented
quirks: `limit` is clamped silently, `page` is clamped silently, an unknown
catalog id is `200 []`, an unknown list is `404`, a non-PRO token gets
`200 {"error": "premium_only"}`. Access tokens expire on a configurable
lifetime, so refresh is exercised for real.

An admin API under /_admin/ edits lists (moving `updated_at` and the account's
activity stamp exactly as a simkl.com edit does) and injects faults.

    python tests/support/fake_simkl.py --port 9000 --refresh-token e2e-refresh
"""
import argparse
import json
import random
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MAX_READABLE = 10000


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class State:
    def __init__(self, refresh_token, token_lifetime, premium=True):
        self.lock = threading.Lock()
        self.refresh_token = refresh_token
        self.token_lifetime = token_lifetime
        self.premium = premium
        self.access = {}              # token -> expiry (epoch)
        self.lists = {}               # id -> {media_type, type, updated_at, items}
        self.catalog = {}             # "tv:1" -> ids
        self.activity = None          # null until the first list edit, as on Simkl
        self.faults = {"rate": 0.0, "kinds": ["429", "503"], "page_max": 500}
        self.stats = {"requests": 0, "faults": 0, "by_path": {}, "catalog_with_auth": 0,
                      "refreshes": 0, "unauthorised": 0}

    def edit(self, list_id, fn):
        with self.lock:
            lst = self.lists[list_id]
            fn(lst)
            lst["updated_at"] = self.activity = now_iso()


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        server_version = "fake-simkl"

        def log_message(self, *a):
            pass

        def _send(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _authed(self):
            auth = self.headers.get("Authorization", "")
            tok = auth[7:] if auth.startswith("Bearer ") else None
            with state.lock:
                exp = state.access.get(tok)
            if exp is None or exp < time.time():
                state.stats["unauthorised"] += 1
                self._send(401, {"error": "invalid_token"})
                return False
            return True

        def _fault(self):
            """Maybe answer with an injected failure instead. Returns True if it did."""
            f = state.faults
            if f["rate"] <= 0 or random.random() >= f["rate"]:
                return False
            state.stats["faults"] += 1
            kind = random.choice(f["kinds"])
            if kind == "drop":
                self.close_connection = True
                self.connection.close()
                return True
            if kind == "slow":
                time.sleep(2)
                return False
            status = int(kind)
            self._send(status, {"error": "rate_limit" if status == 429 else "server_error"})
            return True

        # -- API --------------------------------------------------------------

        def do_POST(self):
            path = urlsplit(self.path).path
            if path.startswith("/_admin/"):
                return self._admin("POST", path, json.loads(self._body() or b"{}"))
            if path == "/oauth2/token":
                form = parse_qs(self._body().decode())
                grant = (form.get("grant_type") or [""])[0]
                if grant != "refresh_token" or (form.get("refresh_token") or [""])[0] != state.refresh_token:
                    return self._send(400, {"error": "invalid_grant"})
                tok = "fake_at_" + secrets.token_hex(12)
                with state.lock:
                    state.access = {tok: time.time() + state.token_lifetime}  # one live token per grant
                    state.stats["refreshes"] += 1
                return self._send(200, {"access_token": tok, "token_type": "Bearer",
                                        "expires_in": state.token_lifetime,
                                        "refresh_token": state.refresh_token, "scope": "media:read"})
            return self._send(404, {"error": "not_found"})

        def do_GET(self):
            parts = urlsplit(self.path)
            path, q = parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}
            if path.startswith("/_admin/"):
                return self._admin("GET", path, {})
            state.stats["requests"] += 1
            state.stats["by_path"][re.sub(r"\d+", "N", path)] = state.stats["by_path"].get(
                re.sub(r"\d+", "N", path), 0) + 1
            if not q.get("client_id") or not self.headers.get("User-Agent"):
                return self._send(400, {"error": "client_id_missing"})
            if self._fault():
                return
            m = re.fullmatch(r"/(tv|anime|movies)/(\d+)", path)
            if m:
                if self.headers.get("Authorization"):
                    state.stats["catalog_with_auth"] += 1
                kind = {"tv": "tv", "anime": "anime", "movies": "movie"}[m.group(1)]
                ids = state.catalog.get(f"{kind}:{m.group(2)}")
                return self._send(200, {"ids": ids} if ids else [])
            if not self._authed():
                return
            if not state.premium:
                return self._send(200, {"error": "premium_only", "message": "PRO", "item": {}})
            if path == "/sync/activities":
                a = state.activity
                return self._send(200, {"all": a, "custom_lists": {"lists": {"all": a, "regular": a}}})
            if path == "/users/settings":
                return self._send(200, {"account": {"id": 1, "type": "vip"}})
            m = re.fullmatch(r"/lists/(\d+)", path)
            if m:
                return self._list(int(m.group(1)), q)
            return self._send(404, {"error": "not_found"})

        def _list(self, list_id, q):
            with state.lock:
                lst = state.lists.get(list_id)
                if lst is None:
                    return self._send(404, {"error": "not_found", "code": 404})
                snapshot = dict(lst, items=list(lst["items"]))
            limit = max(1, min(int(q.get("limit", 50)), 500, state.faults["page_max"]))
            total = len(snapshot["items"])
            pages = max(1, -(-total // limit))
            reachable = max(1, MAX_READABLE // limit)
            page = max(1, min(int(q.get("page", 1)), pages, reachable))    # clamped silently
            items = snapshot["items"][(page - 1) * limit: page * limit]
            return self._send(200, {
                "id": list_id, "name": f"list {list_id}", "media_type": snapshot["media_type"],
                "type": snapshot.get("type", "regular"), "updated_at": snapshot["updated_at"],
                "pagination": {"page": page, "limit": limit, "total_items": total, "total_pages": pages},
                "items": items})

        # -- admin ------------------------------------------------------------

        def _admin(self, method, path, body):
            m = re.fullmatch(r"/_admin/list/(\d+)(/add|/remove|/touch)?", path)
            if path == "/_admin/stats":
                return self._send(200, state.stats)
            if path == "/_admin/faults" and method == "POST":
                state.faults.update(body)
                return self._send(200, state.faults)
            if path == "/_admin/premium" and method == "POST":
                state.premium = bool(body.get("premium"))
                return self._send(200, {"premium": state.premium})
            if path == "/_admin/catalog" and method == "POST":
                with state.lock:
                    state.catalog.update(body)
                return self._send(200, {"entries": len(state.catalog)})
            if m and method == "GET":
                with state.lock:
                    return self._send(200, state.lists.get(int(m.group(1))) or {})
            if m and method == "POST":
                list_id, op = int(m.group(1)), m.group(2)
                if op is None:
                    with state.lock:
                        state.lists[list_id] = {"media_type": body["media_type"],
                                                "type": body.get("type", "regular"),
                                                "updated_at": now_iso(), "items": body.get("items", [])}
                    return self._send(200, {"ok": True})
                if list_id not in state.lists:
                    return self._send(404, {"error": "no such list"})
                if op == "/add":
                    state.edit(list_id, lambda lst: lst["items"].append(body))
                elif op == "/remove":
                    sid = body["simkl_id"]
                    state.edit(list_id, lambda lst: lst.__setitem__(
                        "items", [i for i in lst["items"] if i["ids"].get("simkl_id") != sid]))
                else:
                    state.edit(list_id, lambda lst: None)
                return self._send(200, {"ok": True, "updated_at": state.lists[list_id]["updated_at"]})
            return self._send(404, {"error": "unknown admin route"})

    return H


def serve(port, refresh_token, token_lifetime, host="0.0.0.0"):
    state = State(refresh_token, token_lifetime)
    srv = ThreadingHTTPServer((host, port), make_handler(state))
    return srv, state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--refresh-token", default="e2e-refresh")
    ap.add_argument("--token-lifetime", type=int, default=7 * 86400)
    a = ap.parse_args()
    srv, _ = serve(a.port, a.refresh_token, a.token_lifetime)
    print(f"fake-simkl on :{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
