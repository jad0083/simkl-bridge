"""List caching, id caching, and the rule that an error is never an empty list."""
import json

import pytest

from simkl_bridge.resolve import IdCache, Resolver
from simkl_bridge.service import Forbidden, ListService, WrongTarget
from simkl_bridge.simkl import ListNotFound, Simkl, SimklError
from simkl_bridge.tokens import TokenStore


def build(tmp_path, fake, clock, allowed=None, min_refresh=900):
    tokens = TokenStore(tmp_path / "token.json", refresh_token="simkl_rt_R", client_id="cid",
                        client_secret=None, transport=fake, clock=clock)
    simkl = Simkl("cid", tokens, transport=fake, clock=clock, sleep=clock.sleep)
    resolver = Resolver(simkl, IdCache(tmp_path / "ids.json", clock=clock), clock=clock)
    return ListService(simkl, resolver, clock=clock, min_refresh=min_refresh, allowed=allowed)


def show(n):
    return {"title": f"S{n}", "type": "tv", "ids": {"simkl_id": n, "imdb": f"tt{n}"}}


def serve_list(fake, items, media_type="tv", updated="2026-09-16T05:47:25Z"):
    state = {"items": items, "updated": updated, "media_type": media_type}

    def handler(req):
        limit = int(req["query"].get("limit", 50))
        return 200, {"id": 7, "media_type": state["media_type"], "updated_at": state["updated"],
                     "pagination": {"page": 1, "limit": limit, "total_items": len(state["items"]),
                                    "total_pages": 1},
                     "items": state["items"][:limit]}

    fake.on("GET", "/lists/7", handler)
    return state


def serve_tv_ids(fake, mapping):
    for n, tvdb in mapping.items():
        fake.json("GET", f"/tv/{n}", {"ids": {"simkl": n, "tvdb": tvdb, "imdb": f"tt{n}"}})


def full_reads(fake):
    return [r for r in fake.calls("/lists/7") if r["query"].get("limit") == "500"]


def test_sonarr_feed(tmp_path, fake, clock):
    serve_list(fake, [show(1), show(2)])
    serve_tv_ids(fake, {1: "101", 2: "102"})
    feed = build(tmp_path, fake, clock).feed("sonarr", 7)
    assert feed == [{"title": "S1", "tvdbId": 101, "imdbId": "tt1"},
                    {"title": "S2", "tvdbId": 102, "imdbId": "tt2"}]


def test_items_with_no_tvdb_are_dropped_not_fatal(tmp_path, fake, clock):
    serve_list(fake, [show(1), show(2)])
    serve_tv_ids(fake, {1: "101", 2: None})
    assert [e["tvdbId"] for e in build(tmp_path, fake, clock).feed("sonarr", 7)] == [101]


def test_within_min_refresh_the_list_is_not_reread(tmp_path, fake, clock):
    serve_list(fake, [show(1)])
    serve_tv_ids(fake, {1: "101"})
    svc = build(tmp_path, fake, clock)
    svc.feed("sonarr", 7)
    clock.now += 60
    svc.feed("sonarr", 7)
    assert len(fake.calls("/lists/7")) == 1


def test_after_min_refresh_an_unchanged_list_costs_one_small_read(tmp_path, fake, clock):
    serve_list(fake, [show(1)])
    serve_tv_ids(fake, {1: "101"})
    svc = build(tmp_path, fake, clock)
    svc.feed("sonarr", 7)
    clock.now += 1000
    svc.feed("sonarr", 7)
    assert len(full_reads(fake)) == 1
    assert fake.calls("/lists/7")[-1]["query"]["limit"] == "1"


def test_a_changed_list_is_reread(tmp_path, fake, clock):
    state = serve_list(fake, [show(1)])
    serve_tv_ids(fake, {1: "101", 2: "102"})
    svc = build(tmp_path, fake, clock)
    svc.feed("sonarr", 7)
    state["items"] = [show(1), show(2)]
    state["updated"] = "2026-09-17T00:00:00Z"
    clock.now += 1000
    assert [e["tvdbId"] for e in svc.feed("sonarr", 7)] == [101, 102]


def test_ids_are_looked_up_once_and_survive_a_restart(tmp_path, fake, clock):
    serve_list(fake, [show(1)])
    serve_tv_ids(fake, {1: "101"})
    build(tmp_path, fake, clock).feed("sonarr", 7)
    build(tmp_path, fake, clock).feed("sonarr", 7)
    assert len(fake.calls("/tv/1")) == 1
    assert json.loads((tmp_path / "ids.json").read_text())


def test_a_missing_mapping_is_retried_after_a_week(tmp_path, fake, clock):
    serve_list(fake, [show(1)])
    serve_tv_ids(fake, {1: None})
    svc = build(tmp_path, fake, clock, min_refresh=0)
    svc.feed("sonarr", 7)
    clock.now += 86400
    svc.feed("sonarr", 7)
    assert len(fake.calls("/tv/1")) == 1
    serve_tv_ids(fake, {1: "101"})
    clock.now += 7 * 86400
    assert svc.feed("sonarr", 7) == [{"title": "S1", "tvdbId": 101, "imdbId": "tt1"}]


def test_a_failed_lookup_fails_the_whole_feed(tmp_path, fake, clock):
    """A partial list reads as removals to an arr set to clean its library."""
    serve_list(fake, [show(1), show(2)])
    serve_tv_ids(fake, {1: "101"})
    fake.json("GET", "/tv/2", {"error": "boom"}, status=503)
    with pytest.raises(SimklError):
        build(tmp_path, fake, clock).feed("sonarr", 7)


def test_an_upstream_failure_after_success_is_still_an_error(tmp_path, fake, clock):
    serve_list(fake, [show(1)])
    serve_tv_ids(fake, {1: "101"})
    svc = build(tmp_path, fake, clock)
    svc.feed("sonarr", 7)
    fake.json("GET", "/lists/7", {"error": "boom"}, status=502)
    clock.now += 1000
    with pytest.raises(SimklError):
        svc.feed("sonarr", 7)


def test_a_movie_list_sent_to_sonarr_is_a_config_error(tmp_path, fake, clock):
    serve_list(fake, [], media_type="movies")
    with pytest.raises(WrongTarget, match="radarr"):
        build(tmp_path, fake, clock).feed("sonarr", 7)


def test_a_tv_list_sent_to_radarr_is_a_config_error(tmp_path, fake, clock):
    serve_list(fake, [show(1)])
    with pytest.raises(WrongTarget, match="sonarr"):
        build(tmp_path, fake, clock).feed("radarr", 7)


def test_an_anime_list_is_split_between_the_two(tmp_path, fake, clock):
    items = [{"title": "Series", "type": "anime", "anime_type": "tv", "ids": {"simkl_id": 1}},
             {"title": "Film", "type": "anime", "anime_type": "movie", "ids": {"simkl_id": 2}}]
    serve_list(fake, items, media_type="anime")
    fake.json("GET", "/anime/1", {"ids": {"simkl": 1, "tvdb": "348545"}})
    fake.json("GET", "/anime/2", {"ids": {"simkl": 2, "tmdb": "635302", "imdb": "tt11032374"}})
    svc = build(tmp_path, fake, clock)
    assert svc.feed("sonarr", 7) == [{"title": "Series", "tvdbId": 348545, "imdbId": None}]
    assert svc.feed("radarr", 7) == [{"id": 635302, "imdb_id": "tt11032374", "title": "Film"}]


def test_an_empty_list_is_an_empty_feed(tmp_path, fake, clock):
    serve_list(fake, [])
    assert build(tmp_path, fake, clock).feed("sonarr", 7) == []


def test_the_allowlist_is_enforced_before_any_call(tmp_path, fake, clock):
    with pytest.raises(Forbidden):
        build(tmp_path, fake, clock, allowed={8}).feed("sonarr", 7)
    assert fake.requests == []


def test_not_found_propagates(tmp_path, fake, clock):
    fake.json("GET", "/lists/7", {"error": "not_found"}, status=404)
    with pytest.raises(ListNotFound):
        build(tmp_path, fake, clock).feed("sonarr", 7)


def test_ids_already_on_the_list_item_need_no_lookup(tmp_path, fake, clock):
    item = {"title": "ST", "type": "tv", "ids": {"simkl_id": 1, "imdb": "tt4574334", "tvdb": "305288"}}
    serve_list(fake, [item])
    assert build(tmp_path, fake, clock).feed("sonarr", 7) == [
        {"title": "ST", "tvdbId": 305288, "imdbId": "tt4574334"}]
    assert fake.calls("/tv/1") == []


def test_an_unknown_catalog_id_drops_the_item_not_the_feed(tmp_path, fake, clock):
    serve_list(fake, [show(1), show(2)])
    serve_tv_ids(fake, {1: "101"})
    fake.json("GET", "/tv/2", [])
    assert [e["tvdbId"] for e in build(tmp_path, fake, clock).feed("sonarr", 7)] == [101]


def test_a_list_with_no_updated_at_is_always_reread(tmp_path, fake, clock):
    state = serve_list(fake, [show(1)], updated=None)
    serve_tv_ids(fake, {1: "101", 2: "102"})
    svc = build(tmp_path, fake, clock)
    svc.feed("sonarr", 7)
    state["items"] = [show(1), show(2)]
    clock.now += 1000
    assert len(svc.feed("sonarr", 7)) == 2


def test_a_list_is_fully_reread_after_a_day_regardless(tmp_path, fake, clock):
    state = serve_list(fake, [show(1)])
    serve_tv_ids(fake, {1: "101", 2: "102"})
    svc = build(tmp_path, fake, clock)
    svc.feed("sonarr", 7)
    state["items"] = [show(1), show(2)]      # changed without updated_at moving (e.g. an auto list)
    clock.now += 1000
    assert len(svc.feed("sonarr", 7)) == 1
    clock.now += 86400
    assert len(svc.feed("sonarr", 7)) == 2


def test_the_id_cache_is_written_once_per_build(tmp_path, fake, clock, monkeypatch):
    serve_list(fake, [show(n) for n in range(1, 6)])
    serve_tv_ids(fake, {n: str(100 + n) for n in range(1, 6)})
    from simkl_bridge import resolve
    writes = []
    real = resolve.write_private
    monkeypatch.setattr(resolve, "write_private", lambda p, t: (writes.append(p), real(p, t)))
    build(tmp_path, fake, clock).feed("sonarr", 7)
    assert len(writes) == 1


def test_lookups_already_made_survive_a_failed_build(tmp_path, fake, clock):
    serve_list(fake, [show(1), show(2)])
    serve_tv_ids(fake, {1: "101"})
    fake.json("GET", "/tv/2", {}, status=503)
    with pytest.raises(SimklError):
        build(tmp_path, fake, clock).feed("sonarr", 7)
    assert "tv:1" in json.loads((tmp_path / "ids.json").read_text())
