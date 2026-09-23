"""Ask Sonarr and Radarr to re-read a list as soon as it changes on Simkl.

Both apps check a Custom List at most every 6 (Sonarr) or 12 (Radarr) hours.
A sync requested for one list *definition* skips that floor, so the watcher
finds each definition whose URL points at this bridge and, when the Simkl list
behind it moves, requests a sync of exactly that definition.

Change detection costs one `/sync/activities` call per tick. That timestamp
moves whenever any of the token owner's own lists changes, and only then are
the watched lists' `updated_at` values read. Lists owned by someone else never
move it, so every watched list is also checked on a slower full cycle.

Failures are logged and retried next tick; they never affect serving.
API keys are sent as headers and never logged.
"""
import json
import re
import threading
import time

from . import APP_NAME, __version__
from .http import urllib_transport

# Where each app keeps a list's URL, and which bridge route it must point at.
URL_FIELD = {"sonarr": "baseUrl", "radarr": "url"}
ROUTE = {name: re.compile(rf"/{name}/([0-9]+)/?$") for name in URL_FIELD}


class ArrClient:
    def __init__(self, name, base_url, api_key, transport=urllib_transport):
        self.name = name
        self._base = base_url.rstrip("/")
        self._key = api_key
        self.transport = transport

    def _call(self, method, path, body=None):
        headers = {"X-Api-Key": self._key, "Accept": "application/json",
                   "User-Agent": f"{APP_NAME}/{__version__}"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        r = self.transport(method, f"{self._base}{path}", headers=headers, data=data, timeout=30)
        # The body is deliberately not echoed: an error page can quote the key.
        if r.status not in (200, 201):
            raise RuntimeError(f"{self.name} {method} {path}: HTTP {r.status}")
        return r.json()

    def lists(self):
        """(definition id, list URL) for every import list, enabled or not."""
        out = []
        for item in self._call("GET", "/api/v3/importlist") or []:
            fields = {f.get("name"): f.get("value") for f in item.get("fields") or []}
            out.append((self.name, item.get("id"), fields.get(URL_FIELD[self.name]) or ""))
        return out

    def sync(self, definition_id):
        self._call("POST", "/api/v3/command",
                   {"name": "ImportListSync", "definitionId": int(definition_id)})


def lists_by_simkl_id(entries):
    """{simkl list id: [definition ids]} for entries whose URL is this app's bridge route."""
    out = {}
    for name, definition, url in entries:
        m = ROUTE[name].search(url)
        if m:
            out.setdefault(int(m.group(1)), []).append(definition)
    return out


class Watcher:
    def __init__(self, simkl, service, arrs, clock=time.time, full_every=3600, log=print):
        self._simkl = simkl
        self._service = service
        self.arrs = arrs
        self._clock = clock
        self._full_every = full_every
        self._log = log
        self._activity = None
        self._last_full = None
        self._seen = {}          # simkl list id -> updated_at at last check

    def tick(self):
        targets = self._discover()
        try:
            activity = self._simkl.activities()
        except Exception as e:  # noqa: BLE001 -- retried next tick
            self._log(f"watch: Simkl activities unavailable, skipping this tick: {e}")
            return
        now = self._clock()
        moved = (((activity.get("custom_lists") or {}).get("lists") or {}).get("all"))
        first = self._activity is None
        full_due = self._last_full is None or now - self._last_full >= self._full_every
        if not first and moved == self._activity and not full_due:
            return
        self._activity = moved
        if full_due:
            self._last_full = now
        for list_id, where in sorted(targets.items()):
            try:
                updated = self._simkl.list_meta(list_id).get("updated_at")
            except Exception as e:  # noqa: BLE001
                self._log(f"watch: list {list_id} unreadable, will retry: {e}")
                continue
            before = self._seen.get(list_id)
            self._seen[list_id] = updated
            if before is None or before == updated:
                continue
            self._service.invalidate(list_id)
            for arr, definition in where:
                try:
                    arr.sync(definition)
                    self._log(f"watch: list {list_id} changed; {arr.name} list #{definition} sync requested")
                except Exception as e:  # noqa: BLE001
                    self._log(f"watch: list {list_id} changed; {arr.name} list #{definition} "
                              f"sync failed, will retry on the next change: {e}")

    def _discover(self):
        """{simkl list id: [(arr, definition id)]} across every reachable arr."""
        out = {}
        for arr in self.arrs:
            try:
                found = lists_by_simkl_id(arr.lists())
            except Exception as e:  # noqa: BLE001
                self._log(f"watch: {arr.name} unreachable: {e}")
                continue
            for list_id, definitions in found.items():
                out.setdefault(list_id, []).extend((arr, d) for d in definitions)
        return out

    def run(self, interval, stop=None):
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 -- the watcher must never die
                self._log(f"watch: tick failed: {e}")
            stop.wait(interval)
