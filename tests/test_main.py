"""Startup wiring: which arrs the watcher talks to comes from the environment."""
import pytest

from simkl_bridge.__main__ import arrs_from_env, watch_interval


def test_no_arr_configured_means_no_watcher():
    assert arrs_from_env({}) == []


def test_each_arr_needs_both_url_and_key():
    arrs = arrs_from_env({"SONARR_URL": "http://sonarr:8989", "SONARR_API_KEY": "k",
                          "RADARR_URL": "http://radarr:7878"})
    assert [a.name for a in arrs] == ["sonarr"]


def test_both_arrs():
    env = {"SONARR_URL": "http://s", "SONARR_API_KEY": "a", "RADARR_URL": "http://r", "RADARR_API_KEY": "b"}
    assert [a.name for a in arrs_from_env(env)] == ["sonarr", "radarr"]


def test_a_url_without_a_key_is_a_startup_error_not_a_silent_skip():
    with pytest.raises(SystemExit, match="SONARR_API_KEY"):
        arrs_from_env({"SONARR_URL": "http://sonarr:8989"}, strict=True)


@pytest.mark.parametrize("raw,expected", [(None, 180), ("60", 60), ("5", 30)])
def test_watch_interval_has_a_floor(raw, expected):
    """Faster than 30s buys nothing and spends the user's shared Simkl quota."""
    assert watch_interval({} if raw is None else {"BRIDGE_WATCH_INTERVAL": raw}) == expected


def test_api_base_defaults_to_simkl():
    from simkl_bridge.__main__ import api_base
    assert api_base({}) == "https://api.simkl.com"


def test_api_base_can_point_at_a_stand_in_for_testing():
    from simkl_bridge.__main__ import api_base
    assert api_base({"SIMKL_API_BASE": "http://fake-simkl:9000/"}) == "http://fake-simkl:9000"
