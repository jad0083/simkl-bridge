"""The bridge is the only owner of its grant: it refreshes, and it persists what it gets."""
import json
import os
import stat

import pytest

from simkl_bridge.http import Response
from simkl_bridge.tokens import AuthError, TokenStore


def store(tmp_path, fake, clock):
    return TokenStore(tmp_path / "token.json", refresh_token="simkl_rt_R",
                      client_id="cid", client_secret=None, transport=fake, clock=clock)


def test_first_use_refreshes_and_persists_privately(tmp_path, fake, clock):
    s = store(tmp_path, fake, clock)
    assert s.access_token() == "simkl_at_ONE"
    form = fake.calls("/oauth2/token")[0]["form"]
    assert form == {"grant_type": "refresh_token", "client_id": "cid", "refresh_token": "simkl_rt_R"}
    saved = json.loads((tmp_path / "token.json").read_text())
    assert saved["access_token"] == "simkl_at_ONE"
    assert saved["expires_at"] == clock.now + 604800
    assert stat.S_IMODE(os.stat(tmp_path / "token.json").st_mode) == 0o600


def test_a_fresh_token_is_reused_across_restarts(tmp_path, fake, clock):
    store(tmp_path, fake, clock).access_token()
    clock.now += 3 * 86400
    assert store(tmp_path, fake, clock).access_token() == "simkl_at_ONE"
    assert len(fake.calls("/oauth2/token")) == 1


def test_refreshes_within_a_day_of_expiry(tmp_path, fake, clock):
    s = store(tmp_path, fake, clock)
    s.access_token()
    clock.now += 604800 - 3600
    s.access_token()
    assert len(fake.calls("/oauth2/token")) == 2


def test_invalidate_forces_a_refresh(tmp_path, fake, clock):
    s = store(tmp_path, fake, clock)
    s.access_token()
    s.invalidate()
    s.access_token()
    assert len(fake.calls("/oauth2/token")) == 2


def test_client_secret_is_sent_when_configured(tmp_path, fake, clock):
    s = TokenStore(tmp_path / "t.json", refresh_token="simkl_rt_R", client_id="cid",
                   client_secret="shh", transport=fake, clock=clock)
    s.access_token()
    assert fake.calls("/oauth2/token")[0]["form"]["client_secret"] == "shh"


def test_a_rejected_refresh_token_is_an_auth_error_without_leaking_it(tmp_path, fake, clock):
    fake.json("POST", "/oauth2/token", {"error": "invalid_grant"}, status=400)
    with pytest.raises(AuthError) as exc:
        store(tmp_path, fake, clock).access_token()
    assert "simkl_rt_R" not in str(exc.value)
    assert "invalid_grant" in str(exc.value)


def test_a_corrupt_state_file_is_replaced_not_fatal(tmp_path, fake, clock):
    (tmp_path / "token.json").write_text("{not json")
    assert store(tmp_path, fake, clock).access_token() == "simkl_at_ONE"


def test_a_token_response_without_access_token_is_an_error(tmp_path, fake, clock):
    fake.on("POST", "/oauth2/token", lambda r: Response(200, {}, b"{}"))
    with pytest.raises(AuthError):
        store(tmp_path, fake, clock).access_token()


def test_a_new_refresh_token_discards_the_old_grants_access_token(tmp_path, fake, clock):
    store(tmp_path, fake, clock).access_token()
    other = TokenStore(tmp_path / "token.json", refresh_token="simkl_rt_DIFFERENT",
                       client_id="cid", transport=fake, clock=clock)
    other.access_token()
    assert len(fake.calls("/oauth2/token")) == 2
    assert "simkl_rt" not in (tmp_path / "token.json").read_text()


def test_invalidate_ignores_a_token_that_was_already_replaced(tmp_path, fake, clock):
    s = store(tmp_path, fake, clock)
    s.access_token()
    s.invalidate("some_older_token")
    s.access_token()
    assert len(fake.calls("/oauth2/token")) == 1


def test_a_missing_expires_in_does_not_refresh_on_every_call(tmp_path, fake, clock):
    fake.json("POST", "/oauth2/token", {"access_token": "simkl_at_X"})
    s = store(tmp_path, fake, clock)
    s.access_token()
    s.access_token()
    assert len(fake.calls("/oauth2/token")) == 1


def test_a_network_failure_is_an_auth_error(tmp_path, clock):
    def down(*a, **k):
        raise OSError("no route to host")
    s = TokenStore(tmp_path / "t.json", refresh_token="r", client_id="cid", transport=down, clock=clock)
    with pytest.raises(AuthError, match="no route"):
        s.access_token()


def test_refresh_goes_to_the_configured_base(tmp_path, fake, clock):
    s = TokenStore(tmp_path / "t.json", refresh_token="r", client_id="cid", transport=fake,
                   clock=clock, base="http://stand-in:9000")
    s.access_token()
    assert fake.requests[0]["url"] == "http://stand-in:9000/oauth2/token"
