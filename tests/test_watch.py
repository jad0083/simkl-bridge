"""Change-driven syncs: when a Simkl list moves, ask Sonarr/Radarr to re-read it now.

Both apps check a Custom List at most every 6 (Sonarr) or 12 (Radarr) hours,
but a sync requested for one list definition skips that floor. The watcher
finds the definitions that point at this bridge and requests exactly those.
"""
import json

import pytest
from conftest import FakeSimkl

from simkl_bridge.resolve import IdCache, Resolver
from simkl_bridge.service import ListService
from simkl_bridge.simkl import Simkl
from simkl_bridge.tokens import TokenStore
from simkl_bridge.watch import ArrClient, Watcher, lists_by_simkl_id


def sonarr_lists(*urls, enabled=True):
    return [{"id": 10 + i, "name": f"l{i}", "implementation": "CustomImport",
             "enableAutomaticAdd": enabled,
             "fields": [{"name": "baseUrl", "value": u}]} for i, u in enumerate(urls)]


def radarr_lists(*urls):
    return [{"id": 20 + i, "name": f"r{i}", "implementation": "RadarrListImport", "enabled": True,
             "fields": [{"name": "url", "value": u}]} for i, u in enumerate(urls)]


@pytest.fixture
def sonarr():
    f = FakeSimkl()
    f.json("POST", "/api/v3/command", {"id": 1, "name": "ImportListSync"}, status=201)
    return f


@pytest.fixture
def radarr():
    f = FakeSimkl()
    f.json("POST", "/api/v3/command", {"id": 1, "name": "ImportListSync"}, status=201)
    return f


def test_discovers_only_lists_pointing_at_this_bridge(sonarr):
    sonarr.json("GET", "/api/v3/importlist", sonarr_lists(
        "http://simkl-bridge:8080/sonarr/152642",
        "http://simkl-bridge:8080/sonarr/7/",
        "http://other:8080/radarr/9",          # wrong route for this app
        "https://example.com/list.json"))
    arr = ArrClient("sonarr", "http://sonarr:8989", "KEY", transport=sonarr)
    assert lists_by_simkl_id(arr.lists()) == {152642: [10], 7: [11]}
    assert sonarr.requests[0]["headers"]["X-Api-Key"] == "KEY"


def test_radarr_reads_its_own_field_name(radarr):
    radarr.json("GET", "/api/v3/importlist", radarr_lists("http://simkl-bridge:8080/radarr/5"))
    arr = ArrClient("radarr", "http://radarr:7878", "K", transport=radarr)
    assert lists_by_simkl_id(arr.lists()) == {5: [20]}


def test_sync_requests_one_definition(sonarr):
    ArrClient("sonarr", "http://sonarr:8989/", "K", transport=sonarr).sync(10)
    req = sonarr.calls("/api/v3/command")[0]
    assert req["url"] == "http://sonarr:8989/api/v3/command"
    assert json.loads(req["body"]) == {"name": "ImportListSync", "definitionId": 10}


def test_a_rejected_sync_raises(sonarr):
    sonarr.json("POST", "/api/v3/command", {"message": "Unauthorized"}, status=401)
    with pytest.raises(RuntimeError, match="401"):
        ArrClient("sonarr", "http://sonarr:8989", "K", transport=sonarr).sync(10)


# --- the watcher --------------------------------------------------------------------------

class World:
    def __init__(self, tmp_path, fake, clock, sonarr, radarr):
        self.fake, self.clock, self.sonarr, self.radarr = fake, clock, sonarr, radarr
        self.activity = "2026-09-23T00:00:00Z"
        self.updated = {152642: "u1", 7: "v1"}
        fake.on("GET", "/sync/activities",
                lambda r: (200, {"all": self.activity, "custom_lists": {"lists": {"all": self.activity}}}))
        for lid in self.updated:
            fake.on("GET", f"/lists/{lid}", self._list(lid))
        sonarr.json("GET", "/api/v3/importlist", sonarr_lists("http://simkl-bridge:8080/sonarr/152642"))
        radarr.json("GET", "/api/v3/importlist", radarr_lists("http://simkl-bridge:8080/radarr/7"))
        tokens = TokenStore(tmp_path / "t.json", refresh_token="r", client_id="cid", transport=fake, clock=clock)
        self.simkl = Simkl("cid", tokens, transport=fake, clock=clock, sleep=clock.sleep)
        self.service = ListService(self.simkl, Resolver(self.simkl, IdCache(tmp_path / "i.json", clock=clock),
                                                        clock=clock), clock=clock)
        self.logs = []
        self.watcher = Watcher(self.simkl, self.service,
                               [ArrClient("sonarr", "http://sonarr:8989", "SKEY", transport=sonarr),
                                ArrClient("radarr", "http://radarr:7878", "RKEY", transport=radarr)],
                               clock=clock, full_every=3600, log=self.logs.append)

    def _list(self, lid):
        def handler(req):
            return 200, {"id": lid, "media_type": "anime", "updated_at": self.updated[lid],
                         "pagination": {"page": 1, "limit": 1, "total_items": 0, "total_pages": 1},
                         "items": []}
        return handler

    def syncs(self, arr):
        return [json.loads(r["body"])["definitionId"] for r in arr.calls("/api/v3/command")]


@pytest.fixture
def world(tmp_path, fake, clock, sonarr, radarr):
    return World(tmp_path, fake, clock, sonarr, radarr)


def test_first_tick_only_records_a_baseline(world):
    world.watcher.tick()
    assert world.syncs(world.sonarr) == [] and world.syncs(world.radarr) == []


def test_nothing_changed_costs_one_activities_call(world):
    world.watcher.tick()
    before = len(world.fake.requests)
    world.clock.now += 180
    world.watcher.tick()
    new = [r["path"] for r in world.fake.requests[before:]]
    assert new == ["/sync/activities"]


def test_a_changed_list_syncs_exactly_its_definition(world):
    world.watcher.tick()
    world.activity, world.updated[152642] = "2026-09-23T01:00:00Z", "u2"
    world.clock.now += 180
    world.watcher.tick()
    assert world.syncs(world.sonarr) == [10]
    assert world.syncs(world.radarr) == []
    assert any("152642" in m and "sonarr" in m for m in world.logs)


def test_a_change_invalidates_the_cached_list(world):
    """The arr fetches right after the trigger; it must not get the old copy."""
    world.fake.on("GET", "/lists/152642", lambda r: (200, {
        "id": 152642, "media_type": "anime", "updated_at": world.updated[152642],
        "pagination": {"page": 1, "limit": 500, "total_items": 0, "total_pages": 1}, "items": []}))
    world.service.feed("sonarr", 152642)
    reads = len(world.fake.calls("/lists/152642"))
    world.watcher.tick()
    world.activity, world.updated[152642] = "later", "u2"
    world.watcher.tick()
    world.service.feed("sonarr", 152642)          # within min_refresh: would be cached
    full = [r for r in world.fake.calls("/lists/152642")[reads:] if r["query"].get("limit") == "500"]
    assert full, "the list was served from a stale cache after a detected change"


def test_foreign_lists_are_still_checked_hourly(world):
    """/sync/activities only moves for the token owner's own lists."""
    world.watcher.tick()
    world.updated[7] = "v2"                       # changed, activity did not move
    world.clock.now += 600
    world.watcher.tick()
    assert world.syncs(world.radarr) == []
    world.clock.now += 3600
    world.watcher.tick()
    assert world.syncs(world.radarr) == [20]


def test_a_newly_added_arr_list_is_baselined_not_triggered(world):
    """Saving a list in the arr already syncs it; a second trigger would be noise."""
    world.watcher.tick()
    world.sonarr.json("GET", "/api/v3/importlist", sonarr_lists(
        "http://simkl-bridge:8080/sonarr/152642", "http://simkl-bridge:8080/sonarr/7"))
    world.activity = "moved"
    world.watcher.tick()
    assert world.syncs(world.sonarr) == []


def test_an_unreachable_arr_does_not_stop_the_other(world):
    def down(*a, **k):
        raise OSError("connection refused")
    world.watcher.arrs[0].transport = down
    world.watcher.tick()
    world.activity, world.updated[7] = "moved", "v2"
    world.watcher.tick()
    assert world.syncs(world.radarr) == [20]
    assert any("sonarr" in m and "connection refused" in m for m in world.logs)


def test_a_simkl_failure_skips_the_tick_and_keeps_the_baseline(world):
    world.watcher.tick()
    world.fake.json("GET", "/sync/activities", {"error": "boom"}, status=503)
    world.watcher.tick()
    world.fake.on("GET", "/sync/activities",
                  lambda r: (200, {"custom_lists": {"lists": {"all": "moved"}}}))
    world.updated[152642] = "u2"
    world.watcher.tick()
    assert world.syncs(world.sonarr) == [10]


def test_api_keys_never_reach_the_log(world):
    world.watcher.tick()
    world.activity, world.updated[152642], world.updated[7] = "moved", "u2", "v2"
    world.sonarr.json("POST", "/api/v3/command", {"message": "SKEY is wrong"}, status=400)
    world.watcher.tick()
    assert not any("SKEY" in m or "RKEY" in m for m in world.logs)


# --- review findings ------------------------------------------------------------------------

def test_first_edit_to_a_list_added_after_startup_is_triggered(world):
    """A brand-new Simkl list that nothing watched before."""
    world.updated[99] = "n1"
    world.fake.on("GET", "/lists/99", world._list(99))
    world.watcher.tick()
    world.sonarr.json("GET", "/api/v3/importlist", sonarr_lists(
        "http://simkl-bridge:8080/sonarr/152642", "http://simkl-bridge:8080/sonarr/99"))
    world.clock.now += 180
    world.watcher.tick()                       # first sight of list 99, activity unchanged
    world.activity, world.updated[99] = "moved", "n2"
    world.clock.now += 180
    world.watcher.tick()
    assert world.syncs(world.sonarr) == [11]


def test_a_failed_list_read_is_retried_next_tick_not_next_hour(world):
    world.watcher.tick()
    real = world.fake.routes[("GET", "/lists/152642")]
    world.fake.json("GET", "/lists/152642", {"error": "boom"}, status=503)
    world.activity, world.updated[152642] = "moved", "u2"
    world.clock.now += 180
    world.watcher.tick()
    assert world.syncs(world.sonarr) == []
    world.fake.on("GET", "/lists/152642", real)
    world.clock.now += 180
    world.watcher.tick()
    assert world.syncs(world.sonarr) == [10]


def test_an_arr_that_was_down_still_gets_its_trigger(world):
    """A list feeding both apps: the one that was unreachable must not lose the change."""
    world.sonarr.json("GET", "/api/v3/importlist", sonarr_lists("http://simkl-bridge:8080/sonarr/7"))
    world.watcher.tick()
    world.sonarr.json("POST", "/api/v3/command", {}, status=503)
    world.activity, world.updated[7] = "moved", "v2"
    world.clock.now += 180
    world.watcher.tick()
    assert world.syncs(world.radarr) == [20]
    failed = len(world.sonarr.calls("/api/v3/command"))
    world.sonarr.json("POST", "/api/v3/command", {"id": 2}, status=201)
    world.clock.now += 180
    world.watcher.tick()
    assert world.syncs(world.sonarr)[failed:] == [10], "the change was dropped for the app that was down"
    assert world.syncs(world.radarr) == [20], "radarr must not be triggered twice"


def test_missing_activity_field_falls_back_to_hourly_not_every_tick(world):
    world.fake.on("GET", "/sync/activities", lambda r: (200, {"all": "x"}))
    world.watcher.tick()
    before = len(world.fake.calls("/lists/152642"))
    for _ in range(5):
        world.clock.now += 180
        world.watcher.tick()
    assert len(world.fake.calls("/lists/152642")) == before
    assert sum("custom_lists" in m for m in world.logs) == 1, "say so once, not every tick"


def test_disabled_arr_lists_are_not_triggered(world, sonarr):
    lists = sonarr_lists("http://simkl-bridge:8080/sonarr/152642", enabled=False)
    world.sonarr.json("GET", "/api/v3/importlist", lists)
    world.watcher.tick()
    world.activity, world.updated[152642] = "moved", "u2"
    world.clock.now += 180
    world.watcher.tick()
    assert world.syncs(world.sonarr) == []


def test_radarr_disabled_uses_its_own_field(radarr):
    radarr.json("GET", "/api/v3/importlist", [
        {"id": 20, "enabled": False, "fields": [{"name": "url", "value": "http://b/radarr/5"}]},
        {"id": 21, "enabled": True, "fields": [{"name": "url", "value": "http://b/radarr/6"}]}])
    arr = ArrClient("radarr", "http://radarr:7878", "K", transport=radarr)
    assert lists_by_simkl_id(arr.lists()) == {6: [21]}


def test_lists_outside_bridge_lists_are_not_watched(world):
    world.service._allowed = {7}
    world.watcher.tick()
    assert world.fake.calls("/lists/152642") == []
