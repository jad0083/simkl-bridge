"""Device-flow bootstrap: the refresh token lands in a private file, never on screen."""
import os
import stat

import pytest

from simkl_bridge.auth_cli import DeviceFlowError, device_flow


def run(tmp_path, fake, clock, **kw):
    out = []
    path = device_flow("cid", tmp_path / "simkl-refresh-token.secret", transport=fake,
                       clock=clock, sleep=clock.sleep, say=out.append, **kw)
    return path, "\n".join(out)


def start(fake, interval=5):
    fake.json("POST", "/oauth2/device", {
        "device_code": "DEVICE-SECRET", "user_code": "BDWP-HQPK",
        "verification_uri": "https://simkl.com/pin",
        "verification_uri_complete": "https://simkl.com/pin?user_code=BDWP-HQPK",
        "expires_in": 900, "interval": interval})


def poll_sequence(fake, responses):
    it = iter(responses)
    fake.on("POST", "/oauth2/token", lambda r: next(it))


GRANTED = (200, {"access_token": "simkl_at_A", "refresh_token": "simkl_rt_SECRETVALUE",
                 "expires_in": 604800, "scope": "media:read"})


def test_happy_path_writes_a_private_file_and_prints_no_secret(tmp_path, fake, clock):
    start(fake)
    poll_sequence(fake, [(400, {"error": "authorization_pending"}), GRANTED])
    path, said = run(tmp_path, fake, clock)
    assert path.read_text() == "simkl_rt_SECRETVALUE"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert "https://simkl.com/pin?user_code=BDWP-HQPK" in said
    for secret in ("simkl_rt_SECRETVALUE", "simkl_at_A", "DEVICE-SECRET"):
        assert secret not in said
    assert "sha256" in said


def test_requests_read_only_scope(tmp_path, fake, clock):
    start(fake)
    poll_sequence(fake, [GRANTED])
    run(tmp_path, fake, clock)
    assert fake.calls("/oauth2/device")[0]["form"]["scope"] == "media:read"
    poll = fake.calls("/oauth2/token")[0]["form"]
    assert poll["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert poll["device_code"] == "DEVICE-SECRET"


def test_slow_down_adds_five_seconds(tmp_path, fake, clock):
    start(fake, interval=5)
    poll_sequence(fake, [(400, {"error": "slow_down"}), (400, {"error": "authorization_pending"}), GRANTED])
    t0 = clock.now
    run(tmp_path, fake, clock)
    assert clock.now - t0 == 5 + 10 + 10


def test_gives_up_at_its_own_deadline(tmp_path, fake, clock):
    """Simkl never sends access_denied; a declined request polls pending forever."""
    start(fake)
    fake.json("POST", "/oauth2/token", {"error": "authorization_pending"}, status=400)
    with pytest.raises(DeviceFlowError, match="not approved"):
        run(tmp_path, fake, clock, deadline=60)
    assert not (tmp_path / "simkl-refresh-token.secret").exists()


def test_expired_code_stops(tmp_path, fake, clock):
    start(fake)
    poll_sequence(fake, [(400, {"error": "expired_token"})])
    with pytest.raises(DeviceFlowError, match="expired"):
        run(tmp_path, fake, clock)


def test_v1_client_is_explained(tmp_path, fake, clock):
    fake.json("POST", "/oauth2/device", {"error": "invalid_client"}, status=401)
    with pytest.raises(DeviceFlowError, match="AUTH V2"):
        run(tmp_path, fake, clock)


def test_refuses_to_overwrite_an_existing_file(tmp_path, fake, clock):
    (tmp_path / "simkl-refresh-token.secret").write_text("old")
    with pytest.raises(DeviceFlowError, match="exists"):
        run(tmp_path, fake, clock)
    assert fake.requests == []


def test_transient_errors_while_polling_keep_polling(tmp_path, fake, clock):
    start(fake)
    poll_sequence(fake, [(503, {}), (429, {"error": "rate_limit"}), (200, []), GRANTED])
    path, _ = run(tmp_path, fake, clock)
    assert path.read_text() == "simkl_rt_SECRETVALUE"


def test_device_flow_uses_the_configured_base(tmp_path, fake, clock):
    start(fake)
    poll_sequence(fake, [GRANTED])
    device_flow("cid", tmp_path / "x.secret", transport=fake, clock=clock, sleep=clock.sleep,
                say=lambda m: None, base="http://stand-in:9000")
    assert {r["url"].split("?")[0] for r in fake.requests} == {
        "http://stand-in:9000/oauth2/device", "http://stand-in:9000/oauth2/token"}


def test_an_unreachable_simkl_is_a_clear_error_not_a_traceback(tmp_path, clock):
    def down(*a, **k):
        raise OSError("Name or service not known")
    with pytest.raises(DeviceFlowError, match="could not reach Simkl"):
        device_flow("cid", tmp_path / "x.secret", transport=down, clock=clock,
                    sleep=clock.sleep, say=lambda m: None)
