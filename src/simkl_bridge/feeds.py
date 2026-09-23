"""The JSON each arr's generic import list reads.

Sonarr "Custom List" (4.x) reads `tvdbId`; v5 also reads `imdbId`.
Radarr "Custom Lists" reads `id` as a TMDb id and drops `id <= 0`. An item
with no TMDb id is left out rather than sent imdb-only: Radarr's StevenLu
path would fall back to a title search and could add the wrong film.
"""


def target_of(item):
    kind = item.get("type")
    if kind == "tv":
        return "sonarr"
    if kind == "movie":
        return "radarr"
    if kind == "anime":
        return "radarr" if item.get("anime_type") == "movie" else "sonarr"
    return None


def _int_id(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def sonarr_entry(item, ids):
    tvdb = _int_id(ids.get("tvdb"))
    if tvdb is None:
        return None
    return {"title": item.get("title"), "tvdbId": tvdb,
            "imdbId": ids.get("imdb") or (item.get("ids") or {}).get("imdb")}


def radarr_entry(item, ids):
    tmdb = _int_id(ids.get("tmdb"))
    if tmdb is None:
        return None
    return {"id": tmdb, "imdb_id": ids.get("imdb") or (item.get("ids") or {}).get("imdb"),
            "title": item.get("title")}


ENTRY = {"sonarr": sonarr_entry, "radarr": radarr_entry}
NEEDS = {"sonarr": "tvdb", "radarr": "tmdb"}
