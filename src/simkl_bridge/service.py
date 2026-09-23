"""Turn one Simkl list into one arr's feed.

Any failure raises. An arr set to clean its library reads a missing or partial
list as "these titles were removed", so an error must never become `[]`.
"""
import sys
import threading
import time
from dataclasses import dataclass

from .feeds import ENTRY, NEEDS, target_of

# What each list media_type can feed.
SERVES = {"tv": {"sonarr"}, "movies": {"radarr"}, "anime": {"sonarr", "radarr"}}
# A list is fully re-read at least this often, whatever updated_at says.
MAX_AGE = 86400


class Forbidden(Exception):
    pass


class WrongTarget(Exception):
    pass


@dataclass
class CachedList:
    updated_at: str
    media_type: str
    items: list
    checked: float
    fetched: float
    kind: str = None


class ListService:
    def __init__(self, simkl, resolver, clock=time.time, min_refresh=900, allowed=None, log=None):
        self._simkl = simkl
        self._resolver = resolver
        self._clock = clock
        self._min_refresh = min_refresh
        self._allowed = allowed
        self._lists = {}
        self._lock = threading.Lock()
        self._log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))

    def feed(self, target, list_id):
        if self._allowed is not None and list_id not in self._allowed:
            raise Forbidden(f"list {list_id} is not in BRIDGE_LISTS")
        with self._lock:
            cached = self._current(list_id)
            if target not in SERVES.get(cached.media_type, set()):
                other = "radarr" if target == "sonarr" else "sonarr"
                raise WrongTarget(f"list {list_id} holds {cached.media_type}; "
                                  f"point {other} at /{other}/{list_id} instead")
            # Rebuilt every time: the id cache makes it cheap, and a memoised
            # feed would never re-check a mapping that was missing last week.
            return self._build(target, list_id, cached.items)

    def _current(self, list_id):
        now = self._clock()
        cached = self._lists.get(list_id)
        if cached and now - cached.checked < self._min_refresh:
            return cached
        # updated_at can be trusted only if it exists, and not for `auto` lists,
        # which are saved filters whose contents move on their own.
        if cached and cached.updated_at and cached.kind != "auto" and now - cached.fetched < MAX_AGE:
            meta = self._simkl.list_meta(list_id)
            if meta.get("updated_at") == cached.updated_at:
                cached.checked = now
                return cached
        meta, items = self._simkl.list_items(list_id)
        fresh = CachedList(meta.get("updated_at"), meta.get("media_type"), items, now, now, meta.get("type"))
        self._lists[list_id] = fresh
        self._log(f"list {list_id}: {len(items)} {fresh.media_type} items, updated {fresh.updated_at}")
        return fresh

    def _build(self, target, list_id, items):
        entry, need = ENTRY[target], NEEDS[target]
        out, skipped = [], []
        try:
            for item in items:
                if target_of(item) != target:
                    continue
                e = entry(item, self._resolver.ids(item, need))
                if e is None:
                    skipped.append(f"{item.get('title')} (simkl {(item.get('ids') or {}).get('simkl_id')})")
                else:
                    out.append(e)
        finally:
            # Keep every lookup already made, even when a later one fails.
            self._resolver.flush()
        if skipped:
            self._log(f"list {list_id} -> {target}: no usable id for {len(skipped)}: " + "; ".join(skipped))
        return out
