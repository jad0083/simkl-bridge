"""Simkl id -> external ids, cached on disk.

List items usually carry the ids already; a catalog lookup happens only when
the one an arr needs is missing. A mapping that exists never changes in
practice, so positive results never expire. A missing one is re-checked after
a week, because Simkl does fill them in.
"""
import json
import threading
import time

from .http import write_private

NEGATIVE_TTL = 7 * 86400


class IdCache:
    def __init__(self, path, clock=time.time):
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._dirty = False
        try:
            self._data = json.loads(path.read_text())
            if not isinstance(self._data, dict):
                self._data = {}
        except (OSError, ValueError):
            self._data = {}

    def get(self, key):
        return self._data.get(key)

    def put(self, key, ids):
        with self._lock:
            self._data[key] = {"ids": ids, "at": self._clock()}
            self._dirty = True

    def flush(self):
        with self._lock:
            if self._dirty:
                write_private(self.path, json.dumps(self._data, sort_keys=True))
                self._dirty = False


class Resolver:
    def __init__(self, simkl, cache, clock=time.time):
        self._simkl = simkl
        self._cache = cache
        self._clock = clock

    def ids(self, item, need):
        """The item's own ids, completed from the catalog if `need` is missing."""
        own = {k: v for k, v in (item.get("ids") or {}).items() if v not in (None, "")}
        if own.get(need):
            return own
        sid = own.get("simkl_id") or own.get("simkl")
        if not sid:
            return own
        key = f"{item['type']}:{int(sid)}"
        hit = self._cache.get(key)
        if hit and (hit["ids"].get(need) or self._clock() - hit["at"] < NEGATIVE_TTL):
            return {**own, **hit["ids"]}
        ids = self._simkl.detail_ids(item["type"], sid)
        self._cache.put(key, ids)
        return {**own, **ids}

    def flush(self):
        self._cache.flush()
