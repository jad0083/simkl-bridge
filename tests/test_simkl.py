"""The Simkl client: required parameters, which calls carry a token, and error shapes."""
import pytest

from simkl_bridge.simkl import ListNotFound, PremiumRequired, Simkl, SimklError
from simkl_bridge.tokens import TokenStore


def client(tmp_path, fake, clock):
    tokens = TokenStore(tmp_path / "token.json", refresh_token="simkl_rt_R",
                        client_id="cid", client_secret=None, transport=fake, clock=clock)
    return Simkl("cid", tokens, transport=fake, clock=clock, sleep=clock.sleep)


def item(n, **kw):
    return {"title": f"T{n}", "year": 2000 + n, "type": "tv",
            "ids": {"simkl_id": n, "slug": f"t{n}", "imdb": f"tt{n:07d}"}, **kw}


def page(items, page_no, total_pages, **meta):
    return {"id": 7, "media_type": "tv", "updated_at": "2026-09-16T05:47:25Z", **meta,
            "pagination": {"page": page_no, "limit": 500, "total_items": 0, "total_pages": total_pages},
            "items": items}


def test_every_request_identifies_the_app(tmp_path, fake, clock):
    fake.json("GET", "/lists/7", page([], 1, 1))
    client(tmp_path, fake, clock).list_meta(7)
    req = fake.calls("/lists/7")[0]
    assert req["query"]["client_id"] == "cid"
    assert req["query"]["app-name"] == "simkl-bridge"
    assert req["query"]["app-version"]
    assert req["headers"]["User-Agent"].startswith("simkl-bridge/")


def test_list_reads_carry_the_bearer_token(tmp_path, fake, clock):
    fake.json("GET", "/lists/7", page([], 1, 1))
    client(tmp_path, fake, clock).list_meta(7)
    assert fake.calls("/lists/7")[0]["headers"]["Authorization"] == "Bearer simkl_at_ONE"
    assert fake.calls("/lists/7")[0]["query"]["limit"] == "1"


def test_catalog_lookups_never_carry_a_token(tmp_path, fake, clock):
    """Simkl asks for this explicitly: a token defeats Cloudflare's cache."""
    fake.json("GET", "/tv/5", {"ids": {"simkl": 5, "tvdb": "121361"}})
    ids = client(tmp_path, fake, clock).detail_ids("tv", 5)
    assert ids["tvdb"] == "121361"
    assert "Authorization" not in fake.calls("/tv/5")[0]["headers"]
    assert fake.calls("/oauth2/token") == []


@pytest.mark.parametrize("kind,path", [("tv", "/tv/5"), ("anime", "/anime/5"), ("movie", "/movies/5")])
def test_detail_uses_the_right_catalog(tmp_path, fake, clock, kind, path):
    fake.json("GET", path, {"ids": {"simkl": 5}})
    client(tmp_path, fake, clock).detail_ids(kind, 5)
    assert fake.calls(path)


def test_all_pages_are_read(tmp_path, fake, clock):
    pages = {"1": page([item(1), item(2)], 1, 2), "2": page([item(3)], 2, 2)}
    for p in pages.values():
        p["pagination"]["total_items"] = 3
    fake.on("GET", "/lists/7", lambda r: (200, pages[r["query"]["page"]]))
    meta, items = client(tmp_path, fake, clock).list_items(7)
    assert [i["title"] for i in items] == ["T1", "T2", "T3"]
    assert meta["media_type"] == "tv"
    assert {r["query"]["limit"] for r in fake.calls("/lists/7")} == {"500"}


def test_premium_only_on_200_is_an_error_not_an_empty_list(tmp_path, fake, clock):
    fake.json("GET", "/lists/7", {"error": "premium_only", "message": "PRO", "item": {}})
    with pytest.raises(PremiumRequired):
        client(tmp_path, fake, clock).list_items(7)


def test_unknown_list_is_not_found(tmp_path, fake, clock):
    fake.json("GET", "/lists/7", {"error": "not_found", "code": 404}, status=404)
    with pytest.raises(ListNotFound):
        client(tmp_path, fake, clock).list_meta(7)


def test_private_list_is_an_error(tmp_path, fake, clock):
    fake.json("GET", "/lists/7", {"error": "private_list"}, status=403)
    with pytest.raises(SimklError, match="private_list"):
        client(tmp_path, fake, clock).list_meta(7)


def test_a_401_refreshes_once_then_retries(tmp_path, fake, clock):
    seen = []

    def lists(req):
        seen.append(req["headers"]["Authorization"])
        return (401, {"error": "invalid_token"}) if len(seen) == 1 else (200, page([], 1, 1))

    tokens = iter(["simkl_at_ONE", "simkl_at_TWO"])
    fake.on("POST", "/oauth2/token", lambda r: (200, {"access_token": next(tokens), "expires_in": 604800}))
    fake.on("GET", "/lists/7", lists)
    client(tmp_path, fake, clock).list_meta(7)
    assert seen == ["Bearer simkl_at_ONE", "Bearer simkl_at_TWO"]


def test_a_persistent_401_is_an_error_not_a_loop(tmp_path, fake, clock):
    fake.json("GET", "/lists/7", {"error": "invalid_token"}, status=401)
    with pytest.raises(SimklError):
        client(tmp_path, fake, clock).list_meta(7)
    assert len(fake.calls("/lists/7")) == 2


def test_server_errors_raise(tmp_path, fake, clock):
    fake.json("GET", "/tv/5", {"error": "boom"}, status=503)
    with pytest.raises(SimklError):
        client(tmp_path, fake, clock).detail_ids("tv", 5)


# --- the list read must be complete or fail -------------------------------------------------

def test_a_list_body_without_items_is_an_error_not_empty(tmp_path, fake, clock):
    fake.json("GET", "/lists/7", {"id": 7, "media_type": "tv", "updated_at": "x"})
    with pytest.raises(SimklError):
        client(tmp_path, fake, clock).list_items(7)


def test_a_clamped_page_is_an_error_not_a_repeat(tmp_path, fake, clock):
    """Simkl clamps page silently; a repeated page would duplicate items and drop the rest."""
    fake.on("GET", "/lists/7", lambda r: (200, page([item(1)], 1, 2, )))
    with pytest.raises(SimklError, match="page"):
        client(tmp_path, fake, clock).list_items(7)


def test_a_list_beyond_the_10000_item_cap_is_refused(tmp_path, fake, clock):
    body = page([item(1)], 1, 25)
    body["pagination"]["total_items"] = 12000
    fake.json("GET", "/lists/7", body)
    with pytest.raises(SimklError, match="10000"):
        client(tmp_path, fake, clock).list_items(7)


def test_a_list_edited_mid_read_is_an_error(tmp_path, fake, clock):
    pages = {"1": page([item(1)], 1, 2), "2": page([item(2)], 2, 2, updated_at="2026-09-17T00:00:00Z")}
    fake.on("GET", "/lists/7", lambda r: (200, pages[r["query"]["page"]]))
    with pytest.raises(SimklError, match="changed"):
        client(tmp_path, fake, clock).list_items(7)


def test_a_short_read_is_an_error(tmp_path, fake, clock):
    body = page([item(1)], 1, 1)
    body["pagination"]["total_items"] = 2
    fake.json("GET", "/lists/7", body)
    with pytest.raises(SimklError, match="expected 2"):
        client(tmp_path, fake, clock).list_items(7)


# --- catalog lookups ------------------------------------------------------------------------

@pytest.mark.parametrize("status,body", [(200, []), (404, {"error": "not_found"})])
def test_an_unknown_catalog_id_is_no_mapping_not_an_error(tmp_path, fake, clock, status, body):
    fake.json("GET", "/tv/5", body, status=status)
    assert client(tmp_path, fake, clock).detail_ids("tv", 5) == {}


# --- transient failures ---------------------------------------------------------------------

@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_errors_are_retried_with_backoff(tmp_path, fake, clock, status):
    replies = iter([(status, {"error": "rate_limit"}), (status, {}), (200, {"ids": {"tvdb": "1"}})])
    fake.on("GET", "/tv/5", lambda r: next(replies))
    t0 = clock.now
    assert client(tmp_path, fake, clock).detail_ids("tv", 5) == {"tvdb": "1"}
    assert clock.now - t0 >= 3


def test_retries_are_bounded(tmp_path, fake, clock):
    fake.json("GET", "/tv/5", {}, status=503)
    with pytest.raises(SimklError):
        client(tmp_path, fake, clock).detail_ids("tv", 5)
    assert len(fake.calls("/tv/5")) == 4


def test_a_network_failure_is_a_simkl_error(tmp_path, clock, fake):
    def down(*a, **k):
        raise OSError("connection refused")
    tokens = TokenStore(tmp_path / "t.json", refresh_token="r", client_id="cid", transport=fake, clock=clock)
    c = Simkl("cid", tokens, transport=down, clock=clock, sleep=clock.sleep)
    with pytest.raises(SimklError, match="connection refused"):
        c.detail_ids("tv", 5)


def test_pacing_leaves_headroom_under_the_limit(tmp_path, fake, clock):
    for n in range(1, 11):
        fake.json("GET", f"/tv/{n}", {"ids": {}})
    c = client(tmp_path, fake, clock)
    t0 = clock.now
    for n in range(1, 11):
        c.detail_ids("tv", n)
    assert (clock.now - t0) / 9 == pytest.approx(0.12, abs=1e-6)
