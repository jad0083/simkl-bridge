"""One-time bootstrap: run the AUTH V2 device flow and stage the refresh token.

The token is written to a 0600 file and never printed; only its length and a
SHA-256 prefix are shown, so the value never reaches the terminal or its scrollback.
"""
import hashlib
import os
import time
import urllib.parse

from . import APP_NAME, __version__
from .http import urllib_transport, write_private

DEVICE_URL = "https://api.simkl.com/oauth2/device"
TOKEN_URL = "https://api.simkl.com/oauth2/token"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class DeviceFlowError(Exception):
    pass


def _post(transport, url, form):
    return transport("POST", url, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": f"{APP_NAME}/{__version__}",
    }, data=urllib.parse.urlencode(form).encode(), timeout=30)


def device_flow(client_id, out_path, transport=urllib_transport, clock=time.monotonic,
                sleep=time.sleep, say=print, deadline=600):
    if os.path.exists(out_path):
        raise DeviceFlowError(f"{out_path} exists; store or shred it first")

    r = _post(transport, DEVICE_URL, {"client_id": client_id, "scope": "media:read"})
    start = r.json()
    start = start if isinstance(start, dict) else {}
    if r.status == 401:
        raise DeviceFlowError("client_id rejected: register an AUTH V2 app "
                              "(simkl.com -> Settings -> Developer, 'TV, devices & command line')")
    if r.status != 200 or "device_code" not in start:
        raise DeviceFlowError(f"device authorization failed: HTTP {r.status} {start.get('error', '')}")

    say("Approve simkl-bridge (read-only) in a browser signed in to your Simkl PRO/VIP account:")
    say(start.get("verification_uri_complete") or
        f"{start['verification_uri']}  code: {start['user_code']}")

    interval = int(start.get("interval") or 5)
    give_up = clock() + min(deadline, int(start.get("expires_in") or 900))
    while True:
        sleep(interval)
        if clock() > give_up:
            raise DeviceFlowError("not approved in time; run it again")
        try:
            r = _post(transport, TOKEN_URL, {"grant_type": GRANT, "client_id": client_id,
                                             "device_code": start["device_code"]})
        except OSError:
            continue
        body = r.json()
        body = body if isinstance(body, dict) else {}
        error = body.get("error")
        if r.status == 200 and body.get("refresh_token"):
            break
        if error == "authorization_pending" or r.status == 429 or r.status >= 500 or r.status == 200:
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "expired_token":
            raise DeviceFlowError("the code expired; run it again")
        raise DeviceFlowError(f"token request failed: HTTP {r.status} {error or ''}")

    token = body["refresh_token"]
    write_private(out_path, token)
    digest = hashlib.sha256(token.encode()).hexdigest()[:8]
    say(f"Granted scope: {body.get('scope')}")
    say(f"Refresh token written to {out_path} (len={len(token)}, sha256={digest}).")
    say("Put it in your secret store, pass it as SIMKL_REFRESH_TOKEN, then shred the file.")
    return out_path
