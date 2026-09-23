# simkl-bridge — design

A bridge that serves Simkl **custom lists** to Sonarr and Radarr as the JSON
their generic import lists already understand.

## Why a bridge is needed

Checked 2026-09-23 against running Sonarr 4.0.20.3012 and Radarr 6.4.4.10685,
and against both projects' `develop` source:

- Both ship a native **Simkl User** import list, but it reads only the five
  status buckets (`/sync/all-items/{type}/{status}`: watching, plan to watch,
  hold, completed, dropped). Neither calls `/lists/`.
- The generic JSON import lists (Sonarr *Custom List*, Radarr *Custom Lists*
  and *StevenLu Custom*) take a URL and nothing else. They cannot send the
  `Authorization: Bearer` header Simkl's custom-list API requires.
- Sonarr 4.x's Custom List needs `tvdbId`, and Radarr's Custom Lists needs a
  TMDb `id`. Simkl's list items usually carry both (`ids.tvdb`, `ids.tmdb`), per
  the `GET /lists/{id}` reference. A catalog lookup fills any gap.
- Radarr's feature request for this (Radarr#10787) was closed in Dec 2024
  because Simkl had no API for it. That API now exists (beta, read-only).

## What it serves

| Route | Consumer | Body |
|---|---|---|
| `GET /sonarr/{list_id}` | Sonarr → *Custom List* | `[{"title", "tvdbId", "imdbId"}]` |
| `GET /radarr/{list_id}` | Radarr → *Custom Lists* | `[{"id": tmdb, "imdb_id", "title"}]` |
| `GET /healthz` | Docker healthcheck | `{"status": "ok"}` |

Radarr's *Custom Lists* reads `id` as a TMDb id. *StevenLu Custom* is
deliberately not a target. It reads only `imdb_id`, and for a film without one
Radarr falls back to a title search that can add the wrong film. So a film with
no TMDb id is left out and logged, and never sent imdb-only.

Sonarr 4.x reads `tvdbId` only. `imdbId` is there for Sonarr v5, which maps
it through Skyhook; 4.x ignores it.

Routing by media type: a list holds one media type. `tv` items go to Sonarr and
`movie` items to Radarr. `anime` items are split on `anime_type`: `movie` goes to
Radarr and everything else (`tv`, `ova`, `ona`, `special`, …) to Sonarr.

## Decisions

**Fail loudly, never serve an empty or partial list on error.** Both arrs can
be set to remove library entries that disappear from their import lists. An
upstream failure served as `[]`, or a short read, would read as removals. Any
failure to read the list or to look up an id returns `502`, which the arrs
record as a failed fetch and retry later. A list read is accepted only if it
proves complete:
- every page has `items` and `pagination`
- the page returned is the page asked for (Simkl clamps silently)
- `updated_at` is the same on every page
- the item count equals `total_items`
- the list is within the API's 10,000-item read cap

`429` and `5xx` are retried after 1, 2 and 4 s. Requests are paced at 0.12 s,
under Simkl's 10 GET/s.

**An item with no mappable id is left out, and logged.** For example, a show
Simkl has no TVDB id for, or a Simkl id the catalog does not know (`200 []` or
`404` on a detail lookup). That is permanent, not transient, so leaving it out
is correct, and the negative lookup is cached like a positive one.

**Ids come from the list item first.** A lookup happens only when the id the
target arr needs is missing. **Lookups are cached on disk and never expire.** Simkl ids map to
TVDB/TMDb/IMDb ids that don't change in practice. Negative results expire
after 7 days, because Simkl does add missing mappings. Lookups use the
Cloudflare-cached catalog endpoints (`/tv/{id}`, `/anime/{id}`,
`/movies/{id}`). These need only the `client_id` and must be sent **without** an
`Authorization` header.

**List reads are gated by `updated_at`.** A request within `BRIDGE_MIN_REFRESH`
seconds (default 900) of the last successful fetch is served from cache.
After that, one `limit=1` read of the list compares `updated_at` and pulls the
whole list only if it moved. The gate is skipped, and the whole list re-read,
in three cases: `updated_at` is missing, the list is an `auto` list (a saved
filter whose contents move on their own), or the last full read is more than a
day old. `/sync/activities` would be cheaper, but it only
tracks the token owner's own lists, and a PRO token can read other users'
public lists by id.

**The bridge is the sole owner of its OAuth grant.** AUTH V2 access tokens last
7 days; the refresh token lasts 180 days and slides forward on every refresh.
Refreshing replaces the grant's access token, so two processes refreshing the
same grant cut each other off. The refresh token is a deployment secret. It is
non-rotating, so the copy in the secret store never goes stale.
The current access token lives only in the bridge's own state directory
(`0600`), refreshed when it is within a day of expiry or on a `401`.

**Read-only scope.** The device flow requests `media:read` only. The bridge
never writes to Simkl.

**No published port needed.** The service is meant to share a Docker network
with Sonarr and Radarr and be reached by container name
(`http://simkl-bridge:8080/...`). It has no authentication of its own, so keep
it off public networks. `BRIDGE_LISTS` optionally restricts which list ids it
will serve.

**Change-driven syncs, not a faster poll.** Sonarr's Custom List has a
hardcoded 6-hour minimum refresh, and Radarr's Custom Lists 12 hours
(`MinRefreshInterval` in each list type). Both run their import-list task
every 5 minutes and skip lists inside that window. `ImportListSyncCommand`
with a `definitionId` goes through `FetchSingleList`, which has no interval
check, so a targeted sync is honoured at any time. This was verified against
a live Sonarr 4.0.20: `201`, then an immediate fetch from the bridge inside
the window.

The watcher therefore watches Simkl, not the clock. Each tick is one
`/sync/activities` call. Its `custom_lists.lists.all` moves only when the
token owner's lists change, and only then are the watched lists'
`updated_at` values read. Lists owned by others never move it, so a full
check runs hourly. On a change the cached list is dropped first, because the
arr fetches immediately after the trigger. Then each definition pointing at
that list is synced. The first sight of a list only records a baseline:
saving a list in the arr already syncs it.

Watcher failures are logged and retried next tick, and never touch serving.
Arr API keys go only in the `X-Api-Key` header. Error responses are not
echoed, because an arr's error page can quote the key. The alternative of
emulating another Sonarr/Radarr (their own list types poll every 5/15
minutes) was rejected: it means imitating an internal API that changes
between versions.

**Standard library only.** No runtime dependencies: `http.server`,
`urllib.request`, `json`. There is nothing to pin or update beyond the Python
base image.

## Token bootstrap

1. Register an AUTH V2 app at simkl.com → Settings → Developer, client type
   **TV, devices & command line**. That gives a public `client_id` and no
   secret. It is not sensitive, but it stays out of git like everything else.
2. `python -m simkl_bridge auth` runs the device flow. It prints the approval URL,
   polls, and writes the refresh token to `$XDG_RUNTIME_DIR/simkl-refresh-token.secret`
   (`0600`), printing only its length and a SHA-256 prefix. The value never
   reaches the terminal, shell history, or process arguments.
3. Put it in a secret store, pass it as `SIMKL_REFRESH_TOKEN`, then shred the file.

## Release

CI runs the tests on every push and pull request. On `main` it publishes
`ghcr.io/jad0083/simkl-bridge:<short-sha>`. The tag names a commit so that a
deployment pinning tag and digest records exactly which code it runs. The
base image and every action are pinned by digest or commit.
