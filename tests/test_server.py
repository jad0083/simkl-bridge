"""HTTP surface: routes and status codes, over a real socket."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from simkl_bridge.server import make_server
from simkl_bridge.service import Forbidden, WrongTarget
from simkl_bridge.simkl import ListNotFound, SimklError


class StubService:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def feed(self, target, list_id):
        self.calls.append((target, list_id))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def serve():
    servers = []

    def start(outcome):
        svc = StubService(outcome)
        srv = make_server(svc, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return svc, f"http://127.0.0.1:{srv.server_address[1]}"

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), json.loads(e.read())


def test_feed_is_json(serve):
    svc, base = serve([{"title": "S", "tvdbId": 1}])
    status, ctype, body = get(base + "/sonarr/7")
    assert (status, body) == (200, [{"title": "S", "tvdbId": 1}])
    assert ctype.startswith("application/json")
    assert svc.calls == [("sonarr", 7)]


def test_radarr_route(serve):
    svc, base = serve([])
    assert get(base + "/radarr/42")[0] == 200
    assert svc.calls == [("radarr", 42)]


def test_empty_feed_is_an_empty_array(serve):
    _, base = serve([])
    assert get(base + "/sonarr/7")[2] == []


def test_health(serve):
    svc, base = serve([])
    assert get(base + "/healthz")[:1] == (200,)
    assert svc.calls == []


@pytest.mark.parametrize("path", ["/", "/lidarr/7", "/sonarr/abc", "/sonarr/", "/sonarr/7/x", "/sonarr/-1"])
def test_unknown_routes_are_404(serve, path):
    svc, base = serve([])
    assert get(base + path)[0] == 404
    assert svc.calls == []


@pytest.mark.parametrize("error,status", [
    (SimklError("upstream"), 502),
    (ListNotFound("no list"), 404),
    (Forbidden("not allowed"), 403),
    (WrongTarget("wrong arr"), 400),
])
def test_errors_are_never_an_empty_list(serve, error, status):
    _, base = serve(error)
    code, _, body = get(base + "/sonarr/7")
    assert code == status
    assert isinstance(body, dict) and body["error"]


def test_an_unexpected_exception_is_a_500_with_no_detail(serve):
    _, base = serve(RuntimeError("secret internals"))
    code, _, body = get(base + "/sonarr/7")
    assert code == 500
    assert "secret internals" not in json.dumps(body)


def test_end_to_end_with_the_real_service(tmp_path, fake, clock):
    """Real ListService behind the real server: an upstream failure reaches the arr as a 502."""
    from simkl_bridge.resolve import IdCache, Resolver
    from simkl_bridge.service import ListService
    from simkl_bridge.simkl import Simkl
    from simkl_bridge.tokens import TokenStore

    tokens = TokenStore(tmp_path / "t.json", refresh_token="r", client_id="cid", transport=fake, clock=clock)
    simkl = Simkl("cid", tokens, transport=fake, clock=clock, sleep=clock.sleep)
    svc = ListService(simkl, Resolver(simkl, IdCache(tmp_path / "ids.json", clock=clock), clock=clock),
                      clock=clock)
    srv = make_server(svc, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        fake.json("GET", "/lists/7", {"id": 7, "media_type": "tv", "updated_at": "u",
                                      "pagination": {"page": 1, "limit": 500, "total_items": 1, "total_pages": 1},
                                      "items": [{"title": "S", "type": "tv",
                                                 "ids": {"simkl_id": 1, "tvdb": "5", "imdb": "tt5"}}]})
        assert get(base + "/sonarr/7")[::2] == (200, [{"title": "S", "tvdbId": 5, "imdbId": "tt5"}])
        fake.json("GET", "/lists/8", {"id": 8}, status=200)
        code, _, body = get(base + "/sonarr/8")
        assert code == 502 and body != []
    finally:
        srv.shutdown()
        srv.server_close()
