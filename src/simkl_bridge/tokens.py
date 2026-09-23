"""AUTH V2 access tokens, owned and refreshed by this process alone.

Access tokens last 7 days. The refresh token lasts 180 days, slides forward on
every refresh, and is non-rotating, so the copy in the secret store stays valid.
Refreshing replaces the grant's access token, so only one process may refresh
a grant. This one does, and keeps the result in its own state directory.
"""
import hashlib
import http.client
import json
import threading
import time
import urllib.parse

from . import APP_NAME, __version__
from .http import urllib_transport, write_private

TOKEN_URL = "https://api.simkl.com/oauth2/token"
REFRESH_MARGIN = 86400
# Used if a token response omits expires_in: assume the documented 7 days.
DEFAULT_LIFETIME = 7 * 86400


class AuthError(Exception):
    pass


class TokenStore:
    def __init__(self, path, refresh_token, client_id, client_secret=None,
                 transport=urllib_transport, clock=time.time):
        self.path = path
        self._refresh_token = refresh_token
        # Ties the saved access token to the grant it came from, without storing the refresh token.
        self._grant = hashlib.sha256(refresh_token.encode()).hexdigest()[:16]
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport
        self._clock = clock
        self._lock = threading.Lock()
        self._state = self._load()

    def _load(self):
        try:
            state = json.loads(self.path.read_text())
            if (isinstance(state.get("access_token"), str)
                    and isinstance(state.get("expires_at"), (int, float))
                    and state.get("grant") == self._grant):
                return state
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def access_token(self):
        with self._lock:
            s = self._state
            if s is None or s["expires_at"] - self._clock() < REFRESH_MARGIN:
                self._refresh()
            return self._state["access_token"]

    def invalidate(self, token=None):
        """Drop the access token after a 401 -- unless it has already been replaced."""
        with self._lock:
            if self._state and (token is None or self._state["access_token"] == token):
                self._state = None

    def _refresh(self):
        form = {"grant_type": "refresh_token", "client_id": self._client_id,
                "refresh_token": self._refresh_token}
        if self._client_secret:
            form["client_secret"] = self._client_secret
        try:
            r = self._transport(
                "POST", TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "User-Agent": f"{APP_NAME}/{__version__}"},
                data=urllib.parse.urlencode(form).encode(), timeout=30)
        except (OSError, http.client.HTTPException) as e:
            raise AuthError(f"token refresh failed: {e}") from e
        body = r.json()
        body = body if isinstance(body, dict) else {}
        token = body.get("access_token")
        if r.status != 200 or not token:
            raise AuthError(f"token refresh failed: HTTP {r.status} {body.get('error') or ''}".strip())
        lifetime = int(body.get("expires_in") or DEFAULT_LIFETIME)
        self._state = {"access_token": token, "expires_at": self._clock() + lifetime,
                       "grant": self._grant}
        write_private(self.path, json.dumps(self._state))
