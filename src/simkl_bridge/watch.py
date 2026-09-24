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
ENABLED_FIELD = {"sonarr": "enableAutomaticAdd", "radarr": "enabled"}
ROUTE = {name: re.compile(rf"/{name}/([0-9]+)/?$") for name in URL_FIELD}
# An accepted sync the app hasn't fetched after this long is requested again.
# The app's own retry would wait out its 6h/12h minimum, measured from its
# last successful sync.
REDELIVER_AFTER = 120
# After this many unanswered re-requests, stop and say so, rather than asking
# every tick forever (e.g. if an app's fetches can't be recognised).
MAX_REDELIVERIES = 5


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
        """(app, definition id, list URL) for every import list the app would actually sync.

        Each app skips a disabled list in a targeted sync anyway (Sonarr on
        `enableAutomaticAdd`, Radarr on `enabled`); filtering here keeps the
        watcher from even asking.
        """
        out = []
        for item in self._call("GET", "/api/v3/importlist") or []:
            if not item.get(ENABLED_FIELD[self.name]):
                continue
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
    """One tick: discover targets, read Simkl's activity stamp, and sync what moved.

    State is kept per (app, definition), not per list, so a list feeding both
    apps is not marked done for one while the other was unreachable. Anything
    that fails -- a list read, a sync, an app that was down -- makes the next
    tick check again, rather than waiting for the next activity change or the
    hourly full check.
    """

    def __init__(self, simkl, service, arrs, clock=time.monotonic, full_every=3600, log=print):
        self._simkl = simkl
        self._service = service
        self.arrs = arrs
        self._clock = clock
        self._full_every = full_every
        self._log = log
        self._activity = None
        self._last_full = None
        self._synced = {}          # (app, definition id) -> list updated_at last synced or baselined
        self._retry = False
        self._unreachable = set()
        self._warned_no_activity = False
        self._pending = {}         # (app, definition) -> (arr, list id, serves at request, requested at, attempts)

    def tick(self):
        targets, recovered = self._discover()
        self._redeliver()
        try:
            activity = self._simkl.activities()
        except Exception as e:  # noqa: BLE001 -- retried next tick
            self._log(f"watch: Simkl activities unavailable, skipping this tick: {e}")
            return
        now = self._clock()
        moved = ((activity.get("custom_lists") or {}).get("lists") or {}).get("all")
        if moved is None and not self._warned_no_activity:
            self._warned_no_activity = True
            self._log("watch: /sync/activities has no custom_lists.lists.all; "
                      "falling back to the full check alone")
        activity_moved = moved is not None and moved != self._activity
        full_due = self._last_full is None or now - self._last_full >= self._full_every
        unseen = any((arr.name, d) not in self._synced for where in targets.values() for arr, d in where)
        if not (activity_moved or full_due or unseen or recovered or self._retry):
            return

        ok = True
        for list_id, where in sorted(targets.items()):
            try:
                updated = self._simkl.list_meta(list_id).get("updated_at")
            except Exception as e:  # noqa: BLE001
                self._log(f"watch: list {list_id} unreadable, will retry: {e}")
                ok = False
                continue
            invalidated = False
            for arr, definition in where:
                key = (arr.name, definition)
                # First sight counts as a change. Sonarr does not sync a list when it
                # is added (only when edited), and after a restart nothing says what
                # changed while the bridge was down; one sync per list settles both.
                if key in self._synced and self._synced[key] == updated:
                    continue
                why = "changed" if key in self._synced else "first seen"
                if not invalidated:
                    # The app fetches right after the trigger; it must not get the old copy.
                    self._service.invalidate(list_id)
                    invalidated = True
                # Counted before the request: an app can fetch before sync() returns.
                seen = self._service.serves(arr.name, list_id)
                try:
                    arr.sync(definition)
                except Exception as e:  # noqa: BLE001
                    self._log(f"watch: list {list_id} {why}; {arr.name} list #{definition} "
                              f"sync failed, will retry: {e}")
                    ok = False
                    continue
                self._synced[key] = updated
                self._pending[key] = (arr, list_id, seen, self._clock(), 0)
                self._log(f"watch: list {list_id} {why}; {arr.name} list #{definition} sync requested")

        # Only a complete pass consumes the activity change or the full check.
        self._retry = not ok
        if ok:
            if moved is not None:
                self._activity = moved
            if full_due:
                self._last_full = now

    def _redeliver(self):
        """Ask again for any accepted sync the app hasn't actually fetched.

        An app accepting the command says nothing about its fetch, which can
        fail -- a Simkl hiccup makes the bridge answer an error, correctly.
        Delivery counts only once the bridge has served that list whole to
        that app since the request.
        """
        now = self._clock()
        for key, (arr, list_id, seen, at, attempts) in list(self._pending.items()):
            if self._service.serves(arr.name, list_id) > seen:
                del self._pending[key]
                continue
            if now - at < REDELIVER_AFTER:
                continue
            if attempts >= MAX_REDELIVERIES:
                del self._pending[key]
                self._log(f"watch: list {list_id}; {arr.name} list #{key[1]} still hasn't fetched after "
                          f"{attempts} re-requests; giving up until the list next changes. "
                          f"Check {arr.name}'s own log for why its fetch fails")
                continue
            seen = self._service.serves(arr.name, list_id)
            try:
                arr.sync(key[1])
            except Exception as e:  # noqa: BLE001
                self._log(f"watch: list {list_id}; {arr.name} list #{key[1]} not fetched since "
                          f"the sync request, and asking again failed, will retry: {e}")
                continue
            self._pending[key] = (arr, list_id, seen, now, attempts + 1)
            self._log(f"watch: list {list_id}; {arr.name} list #{key[1]} not fetched since "
                      f"the sync request, asking again")

    def _discover(self):
        """({simkl list id: [(app, definition id)]}, whether an app just came back)."""
        out, recovered = {}, False
        for arr in self.arrs:
            try:
                found = lists_by_simkl_id(arr.lists())
            except Exception as e:  # noqa: BLE001
                if arr.name not in self._unreachable:
                    self._log(f"watch: {arr.name} unreachable, will retry: {e}")
                self._unreachable.add(arr.name)
                continue
            if arr.name in self._unreachable:
                # Changes made while it was down were never compared for it.
                self._unreachable.discard(arr.name)
                recovered = True
                self._log(f"watch: {arr.name} reachable again")
            for list_id, definitions in found.items():
                if not self._service.allows(list_id):
                    continue
                out.setdefault(list_id, []).extend((arr, d) for d in definitions)
        return out, recovered

    def run(self, interval, stop=None):
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 -- the watcher must never die
                self._log(f"watch: tick failed: {e}")
            stop.wait(interval)
