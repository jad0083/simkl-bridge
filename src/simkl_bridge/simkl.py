"""The subset of the Simkl API this bridge uses."""
import http.client
import threading
import time
import urllib.parse

from . import APP_NAME, __version__
from .http import urllib_transport
from .tokens import AuthError

BASE = "https://api.simkl.com"
PAGE_SIZE = 500
# Simkl rejects `page * limit` beyond this, clamping the page silently.
MAX_READABLE = 10000
# Simkl caps GETs at 10/sec per client_id and per user; stay clear of the edge.
MIN_INTERVAL = 0.12
# 429 and 5xx are retried after 1, 2 and 4 seconds, then the request fails.
BACKOFF = (1, 2, 4)
TRANSIENT = {429, 500, 502, 503, 504}
CATALOG = {"tv": "tv", "anime": "anime", "movie": "movies"}


class SimklError(Exception):
    pass


class PremiumRequired(SimklError):
    pass


class ListNotFound(SimklError):
    pass


class Simkl:
    def __init__(self, client_id, tokens, transport=urllib_transport,
                 clock=time.monotonic, sleep=time.sleep, base=BASE):
        self._client_id = client_id
        self._tokens = tokens
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._base = base
        self._pace = threading.Lock()
        self._last = None

    def list_meta(self, list_id):
        """The list's metadata, including `updated_at`, from a one-item read."""
        path = f"/lists/{int(list_id)}"
        return self._list_body(path, self._get(path, {"limit": 1}, auth=True))

    def list_items(self, list_id):
        """(metadata, every item). Raises unless the read is provably complete.

        A short or duplicated read would reach an arr as a list with titles
        missing, which it may act on by removing them.
        """
        path = f"/lists/{int(list_id)}"
        items, page, first = [], 1, None
        while True:
            body = self._list_body(path, self._get(path, {"limit": PAGE_SIZE, "page": page}, auth=True))
            pg = body["pagination"]
            if first is None:
                first = body
                if int(pg.get("total_items") or 0) > MAX_READABLE:
                    raise SimklError(f"{path}: {pg['total_items']} items; Simkl serves at most "
                                     f"{MAX_READABLE} through the API")
            elif body.get("updated_at") != first.get("updated_at"):
                raise SimklError(f"{path}: list changed while it was being read; retry")
            if pg.get("page") != page:
                raise SimklError(f"{path}: asked for page {page}, got page {pg.get('page')}")
            items.extend(body["items"])
            if page >= int(pg.get("total_pages") or 1):
                break
            page += 1
        expected = int(first["pagination"].get("total_items") or 0)
        if len(items) != expected:
            raise SimklError(f"{path}: read {len(items)} items, expected {expected}")
        return first, items

    def detail_ids(self, kind, simkl_id):
        """External ids for one title, from the Cloudflare-cached catalog.

        Sent without a token on purpose: Simkl asks that these calls carry no
        Authorization header, which would otherwise bypass the edge cache.
        An id Simkl does not know (`200 []` or `404`) is no mapping, not an error.
        """
        path = f"/{CATALOG[kind]}/{int(simkl_id)}"
        r = self._request(path, {}, auth=False)
        body = r.json()
        if r.status == 404 or (r.status == 200 and body == []):
            return {}
        return (self._check(path, r).get("ids")) or {}

    @staticmethod
    def _list_body(path, body):
        if not isinstance(body.get("items"), list) or not isinstance(body.get("pagination"), dict):
            raise SimklError(f"{path}: response has no items/pagination; not treating it as empty")
        return body

    def _get(self, path, params, auth):
        r = self._request(path, params, auth)
        if r.status == 404:
            raise ListNotFound(f"{path}: not found")
        return self._check(path, r)

    def _request(self, path, params, auth):
        refreshed = False
        retries = iter(BACKOFF)
        while True:
            headers = {"User-Agent": f"{APP_NAME}/{__version__}", "Accept": "application/json"}
            token = None
            if auth:
                try:
                    token = self._tokens.access_token()
                except AuthError as e:
                    raise SimklError(str(e)) from e
                headers["Authorization"] = f"Bearer {token}"
            query = {"client_id": self._client_id, "app-name": APP_NAME,
                     "app-version": __version__, **params}
            self._wait_turn()
            try:
                r = self._transport("GET", f"{self._base}{path}?{urllib.parse.urlencode(query)}",
                                    headers=headers, timeout=30)
            except (OSError, http.client.HTTPException) as e:
                raise SimklError(f"{path}: {e}") from e
            if r.status == 401 and auth and not refreshed:
                refreshed = True
                self._tokens.invalidate(token)
                continue
            if r.status in TRANSIENT:
                delay = next(retries, None)
                if delay is not None:
                    self._sleep(delay)
                    continue
            return r

    @staticmethod
    def _check(path, r):
        body = r.json()
        error = body.get("error") if isinstance(body, dict) else None
        if r.status != 200:
            raise SimklError(f"{path}: HTTP {r.status} {error or ''}".strip())
        if error == "premium_only":
            raise PremiumRequired(f"{path}: the token's account is not Simkl PRO or VIP")
        if error or not isinstance(body, dict):
            raise SimklError(f"{path}: unexpected response {error or type(body).__name__}")
        return body

    def _wait_turn(self):
        with self._pace:
            now = self._clock()
            if self._last is not None and now - self._last < MIN_INTERVAL:
                self._sleep(MIN_INTERVAL - (now - self._last))
            self._last = self._clock()
