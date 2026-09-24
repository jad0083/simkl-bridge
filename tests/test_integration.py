"""The real bridge process, over real HTTP, against the fake Simkl.

Unit tests drive the modules through a scripted transport. These start
`python -m simkl_bridge serve` exactly as the container does, point it at
tests/support/fake_simkl.py, and check what an arr would receive.
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))
import fake_simkl


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(url, data=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def show(n, tvdb=None):
    ids = {"simkl_id": n, "slug": f"s{n}", "imdb": f"tt{n:07d}"}
    if tvdb:
        ids["tvdb"] = str(tvdb)
    return {"title": f"Show {n}", "year": 2020, "type": "tv", "ids": ids}


@pytest.fixture
def stack(tmp_path):
    simkl_port, bridge_port = free_port(), free_port()
    srv, state = fake_simkl.serve(simkl_port, "it-refresh", 7 * 86400, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = dict(os.environ, SIMKL_CLIENT_ID="it-client", SIMKL_REFRESH_TOKEN="it-refresh",
               SIMKL_API_BASE=f"http://127.0.0.1:{simkl_port}", BRIDGE_PORT=str(bridge_port),
               BRIDGE_DATA_DIR=str(tmp_path / "data"), BRIDGE_MIN_REFRESH="0",
               PYTHONPATH=str(ROOT / "src"))
    for k in ("SONARR_URL", "SONARR_API_KEY", "RADARR_URL", "RADARR_API_KEY"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, "-m", "simkl_bridge", "serve"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{bridge_port}"
    admin = f"http://127.0.0.1:{simkl_port}/_admin"
    for _ in range(100):
        try:
            if call(base + "/healthz")[0] == 200:
                break
        except OSError:
            time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("bridge did not start: " + proc.stdout.read())
    yield base, admin, state, tmp_path / "data"
    proc.terminate()
    proc.wait(10)
    srv.shutdown()


def test_a_paged_list_is_served_whole(stack):
    base, admin, _state, _ = stack
    call(f"{admin}/faults", {"page_max": 2})
    call(f"{admin}/list/5", {"media_type": "tv", "items": [show(i, 1000 + i) for i in range(1, 8)]})
    status, body = call(f"{base}/sonarr/5")
    assert status == 200
    assert [e["tvdbId"] for e in body] == [1001, 1002, 1003, 1004, 1005, 1006, 1007]


def test_missing_ids_come_from_the_catalog_without_a_token(stack):
    base, admin, state, _ = stack
    call(f"{admin}/catalog", {"tv:1": {"simkl": 1, "tvdb": "81189"}})
    call(f"{admin}/list/6", {"media_type": "tv", "items": [show(1), show(2)]})
    status, body = call(f"{base}/sonarr/6")
    assert status == 200 and body == [{"title": "Show 1", "tvdbId": 81189, "imdbId": "tt0000001"}]
    assert state.stats["catalog_with_auth"] == 0


def test_the_token_is_refreshed_and_kept_private(stack):
    base, admin, state, data = stack
    call(f"{admin}/list/7", {"media_type": "tv", "items": []})
    assert call(f"{base}/sonarr/7") == (200, [])
    token = data / "token.json"
    assert oct(token.stat().st_mode & 0o777) == "0o600"
    assert state.stats["refreshes"] == 1
    state.access.clear()                       # the grant's token is revoked upstream
    assert call(f"{base}/sonarr/7")[0] == 200
    assert state.stats["refreshes"] == 2


def test_a_non_premium_account_is_an_error_not_an_empty_list(stack):
    base, admin, _state, _ = stack
    call(f"{admin}/list/8", {"media_type": "tv", "items": [show(1, 5)]})
    call(f"{admin}/premium", {"premium": False})
    status, body = call(f"{base}/sonarr/8")
    assert status == 502 and "PRO" in body["error"]


def test_unknown_list_and_wrong_app(stack):
    base, admin, _, _ = stack
    call(f"{admin}/list/9", {"media_type": "movies", "items": []})
    assert call(f"{base}/sonarr/404")[0] == 404
    status, body = call(f"{base}/sonarr/9")
    assert status == 400 and "radarr" in body["error"]


def test_under_a_fault_storm_the_answer_is_whole_or_an_error(stack):
    """Never partial: an arr cleaning its library would read a short list as removals."""
    base, admin, _, _ = stack
    items = [show(i, 2000 + i) for i in range(1, 12)]
    call(f"{admin}/list/10", {"media_type": "tv", "items": items})
    call(f"{admin}/faults", {"rate": 0.3, "kinds": ["429", "503", "drop"], "page_max": 3})
    outcomes = {"whole": 0, "error": 0}
    for _ in range(15):
        status, body = call(f"{base}/sonarr/10")
        if status == 200:
            assert len(body) == len(items), f"partial list served: {len(body)} of {len(items)}"
            outcomes["whole"] += 1
        else:
            assert isinstance(body, dict) and body.get("error")
            outcomes["error"] += 1
    assert outcomes["whole"] > 0, "retries never got a whole list through"
