"""What each arr receives: routing by media type, id mapping, and the empty-list rule."""
import pytest

from simkl_bridge.feeds import radarr_entry, sonarr_entry, target_of


def tv(**ids):
    return {"title": "Show", "type": "tv", "ids": {"simkl_id": 1, "imdb": "tt1", **ids}}


def anime(anime_type):
    return {"title": "Anime", "type": "anime", "anime_type": anime_type, "ids": {"simkl_id": 2}}


def movie():
    return {"title": "Film", "type": "movie", "ids": {"simkl_id": 3, "imdb": "tt3"}}


@pytest.mark.parametrize("item,expected", [
    (tv(), "sonarr"),
    (movie(), "radarr"),
    (anime("tv"), "sonarr"),
    (anime("ova"), "sonarr"),
    (anime("ona"), "sonarr"),
    (anime("special"), "sonarr"),
    (anime("movie"), "radarr"),
    ({"title": "?", "type": "manga", "ids": {}}, None),
])
def test_routing(item, expected):
    assert target_of(item) == expected


def test_sonarr_entry_carries_tvdb_as_int_and_imdb():
    assert sonarr_entry(tv(), {"tvdb": "121361", "imdb": "tt0944947"}) == {
        "title": "Show", "tvdbId": 121361, "imdbId": "tt0944947"}


def test_sonarr_entry_falls_back_to_the_list_items_imdb():
    assert sonarr_entry(tv(), {"tvdb": "5"})["imdbId"] == "tt1"


@pytest.mark.parametrize("ids", [{}, {"tvdb": None}, {"tvdb": ""}, {"tvdb": "the-walking-dead"}, {"tvdb": "0"}])
def test_sonarr_entry_without_a_numeric_tvdb_is_dropped(ids):
    assert sonarr_entry(tv(), ids) is None


def test_radarr_entry_carries_tmdb_as_id():
    assert radarr_entry(movie(), {"tmdb": "27205", "imdb": "tt1375666"}) == {
        "id": 27205, "imdb_id": "tt1375666", "title": "Film"}


def test_radarr_entry_without_tmdb_is_dropped_even_with_imdb():
    """Never let Radarr guess: with no TMDb id it would fall back to a title search."""
    assert radarr_entry(movie(), {"imdb": "tt3"}) is None
    assert radarr_entry(anime("movie"), {}) is None


def test_list_item_ids_are_enough_on_their_own():
    item = {"title": "Stranger Things", "type": "tv",
            "ids": {"simkl_id": 548312, "imdb": "tt4574334", "tmdb": "66732", "tvdb": "305288"}}
    assert sonarr_entry(item, item["ids"])["tvdbId"] == 305288
