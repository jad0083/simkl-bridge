"""End to end: real Sonarr and Radarr, the real bridge image, a fake Simkl.

Run through tests/e2e/run.sh, which starts the stack in compose.yaml. Each step
does what a user does -- through Sonarr's and Radarr's own APIs -- and checks
what the user would see: titles actually added to the library.

The fast-sync checks bound the wait at 120 seconds. Sonarr's own minimum for
a Custom List is 6 hours and Radarr's 12, so a title arriving inside that
bound can only have come from the bridge's targeted sync.
"""
import json
import os
import time
import urllib.error
import urllib.request

import pytest

URL_BASE = os.environ.get("URL_BASE", "")
SONARR = f"http://127.0.0.1:18989{URL_BASE}"
RADARR = f"http://127.0.0.1:17878{URL_BASE}"
KEYS = {SONARR: "e2e0sonarr0key000000000000000000", RADARR: "e2e0radarr0key000000000000000000"}
ADMIN = "http://127.0.0.1:19000/_admin"
BRIDGE = "http://127.0.0.1:18080"

# Real titles, so the apps' own metadata lookups succeed.
GOT = {"title": "Game of Thrones", "year": 2011, "type": "tv",
       "ids": {"simkl_id": 17465, "slug": "game-of-thrones", "imdb": "tt0944947", "tvdb": "121361"}}
BREAKING_BAD = {"title": "Breaking Bad", "year": 2008, "type": "tv",
                "ids": {"simkl_id": 11121, "slug": "breaking-bad", "imdb": "tt0903747", "tvdb": "81189"}}
MATRIX = {"title": "The Matrix", "year": 1999, "type": "movie",
          "ids": {"simkl_id": 53992, "slug": "the-matrix", "imdb": "tt0133093", "tmdb": "603"}}
INCEPTION = {"title": "Inception", "year": 2010, "type": "movie",
             "ids": {"simkl_id": 53536, "slug": "inception", "imdb": "tt1375666", "tmdb": "27205"}}
TV_LIST, MOVIE_LIST = 100, 200


def api(method, url, body=None, key=None, expect=(200, 201, 202)):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if key:
        headers["X-Api-Key"] = key
    req = urllib.request.Request(url, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    data = json.loads(raw) if raw else None
    if expect and status not in expect:
        raise AssertionError(f"{method} {url} -> {status}: {str(data)[:400]}")
    return status, data


def arr(base, method, path, body=None, expect=(200, 201, 202)):
    return api(method, f"{base}/api/v3{path}", body, key=KEYS[base], expect=expect)


def wait(what, fn, timeout=180, every=3):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
            if last:
                return last
        except (OSError, AssertionError) as e:
            last = e
        time.sleep(every)
    pytest.fail(f"timed out after {timeout}s waiting for {what}; last: {last!r}")


def series_tvdb_ids():
    return {s["tvdbId"] for s in arr(SONARR, "GET", "/series")[1]}


def movie_tmdb_ids():
    return {m["tmdbId"] for m in arr(RADARR, "GET", "/movie")[1]}


def custom_list(base, implementation, url_field, url, **settings):
    """A new import list built from the app's own schema, as its UI would."""
    schema = next(s for s in arr(base, "GET", "/importlist/schema")[1]
                  if s["implementation"] == implementation)
    body = dict(schema, name=f"e2e {url.rsplit('/', 2)[-2]} {url.rsplit('/', 1)[-1]}", **settings)
    for f in body["fields"]:
        if f["name"] == url_field:
            f["value"] = url
    return body


@pytest.fixture(scope="module")
def ready():
    wait("sonarr", lambda: arr(SONARR, "GET", "/system/status")[1]["version"], timeout=240)
    wait("radarr", lambda: arr(RADARR, "GET", "/system/status")[1]["version"], timeout=240)
    wait("bridge", lambda: api("GET", f"{BRIDGE}/healthz")[1]["status"] == "ok")
    api("POST", f"{ADMIN}/list/{TV_LIST}", {"media_type": "tv", "items": [GOT]})
    api("POST", f"{ADMIN}/list/{MOVIE_LIST}", {"media_type": "movies", "items": [MATRIX]})
    arr(SONARR, "POST", "/rootfolder", {"path": "/tv"})
    arr(RADARR, "POST", "/rootfolder", {"path": "/movies"})
    return {
        "sonarr_profile": arr(SONARR, "GET", "/qualityprofile")[1][0]["id"],
        "radarr_profile": arr(RADARR, "GET", "/qualityprofile")[1][0]["id"],
        "versions": (arr(SONARR, "GET", "/system/status")[1]["version"],
                     arr(RADARR, "GET", "/system/status")[1]["version"]),
    }


@pytest.fixture(scope="module")
def sonarr_list(ready):
    body = custom_list(SONARR, "CustomImport", "baseUrl",
                       f"http://simkl-bridge:8080/sonarr/{TV_LIST}",
                       enableAutomaticAdd=True, shouldMonitor="all", rootFolderPath="/tv",
                       qualityProfileId=ready["sonarr_profile"], seriesType="standard",
                       seasonFolder=True, monitorNewItems="all")
    # Saving validates by fetching the URL -- the app itself accepts the bridge's JSON.
    return arr(SONARR, "POST", "/importlist", body)[1]


@pytest.fixture(scope="module")
def radarr_list(ready):
    body = custom_list(RADARR, "RadarrListImport", "url",
                       f"http://simkl-bridge:8080/radarr/{MOVIE_LIST}",
                       enabled=True, enableAuto=True, monitor="movieOnly",
                       minimumAvailability="released", rootFolderPath="/movies",
                       qualityProfileId=ready["radarr_profile"], searchOnAdd=False)
    return arr(RADARR, "POST", "/importlist", body)[1]


def test_versions_under_test(ready):
    print("Sonarr", ready["versions"][0], "Radarr", ready["versions"][1], "URL base", repr(URL_BASE))


def test_sonarr_adds_the_lists_series(sonarr_list):
    wait("Game of Thrones in Sonarr", lambda: 121361 in series_tvdb_ids())


def test_radarr_adds_the_lists_movie(radarr_list):
    wait("The Matrix in Radarr", lambda: 603 in movie_tmdb_ids())


def test_a_new_title_reaches_sonarr_within_minutes(sonarr_list):
    wait("Game of Thrones first", lambda: 121361 in series_tvdb_ids())
    time.sleep(35)                         # let the watcher record its baseline
    api("POST", f"{ADMIN}/list/{TV_LIST}/add", BREAKING_BAD)
    wait("Breaking Bad via a triggered sync", lambda: 81189 in series_tvdb_ids(), timeout=120)


def test_a_new_title_reaches_radarr_within_minutes(radarr_list):
    wait("The Matrix first", lambda: 603 in movie_tmdb_ids())
    time.sleep(35)
    api("POST", f"{ADMIN}/list/{MOVIE_LIST}/add", INCEPTION)
    wait("Inception via a triggered sync", lambda: 27205 in movie_tmdb_ids(), timeout=120)


def test_a_simkl_outage_never_unmonitors_anything(sonarr_list):
    """List cleaning on, Simkl failing: the bridge must answer errors, not an empty list."""
    wait("series present", lambda: {121361, 81189} <= series_tvdb_ids(), timeout=240)
    cfg = arr(SONARR, "GET", "/config/importlist")[1]
    arr(SONARR, "PUT", f"/config/importlist/{cfg['id']}", dict(cfg, listSyncLevel="keepAndUnmonitor"))
    api("POST", f"{ADMIN}/faults", {"rate": 1.0, "kinds": ["503"]})
    try:
        before = {s["tvdbId"]: s["monitored"] for s in arr(SONARR, "GET", "/series")[1]}
        arr(SONARR, "POST", "/command", {"name": "ImportListSync", "definitionId": sonarr_list["id"]})
        time.sleep(30)
        after = {s["tvdbId"]: s["monitored"] for s in arr(SONARR, "GET", "/series")[1]}
        assert after == before, f"series changed during a Simkl outage: {before} -> {after}"
    finally:
        api("POST", f"{ADMIN}/faults", {"rate": 0.0})
        arr(SONARR, "PUT", f"/config/importlist/{cfg['id']}", dict(cfg, listSyncLevel="disabled"))


def test_a_movie_list_offered_to_sonarr_is_refused_with_a_reason(ready):
    body = custom_list(SONARR, "CustomImport", "baseUrl",
                       f"http://simkl-bridge:8080/sonarr/{MOVIE_LIST}",
                       enableAutomaticAdd=True, shouldMonitor="all", rootFolderPath="/tv",
                       qualityProfileId=ready["sonarr_profile"], seriesType="standard",
                       seasonFolder=True, monitorNewItems="all")
    status, data = arr(SONARR, "POST", "/importlist/test", body, expect=None)
    assert status >= 400, f"Sonarr accepted a movie list: {data}"
