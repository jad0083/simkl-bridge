# Plan

## Bridge
- [x] Serve Simkl custom lists to Sonarr (`/sonarr/{id}`, tvdbId) and Radarr (`/radarr/{id}`, tmdb id + imdb_id)
- [x] AUTH V2 token ownership: refresh 1 day before the 7-day expiry or on 401, persisted 0600
- [x] Device-flow bootstrap (`python -m simkl_bridge auth`) writing the refresh token to a 0600 file, never to screen
- [x] Id lookups via the cached catalog endpoints, persisted; missing mappings re-checked weekly
- [x] List reads gated on `updated_at`; never serve `[]` on an error
- [x] Anime lists split: series/OVA/ONA/special → Sonarr, films → Radarr
- [x] Container image (stdlib only, non-root, healthcheck) built by CI to ghcr.io/jad0083/simkl-bridge

## Faster syncs
- [x] Watch Simkl activity and request single-list syncs from Sonarr/Radarr on change, skipping their 6/12-hour Custom List floors (0.2.1; verified live: an edit on simkl.com reached Sonarr as an added series in 18 seconds)

## Release
- [x] Images also tagged `latest` and `v<version>`, with OCI labels linking the ghcr package to the repo (#1; `latest` = `v0.2.1`)
- [x] README rewritten for people looking to use Simkl custom lists with Sonarr/Radarr (#2); GitHub Release v0.2.1 published
- [x] Public repo `jad0083/simkl-bridge` and first image published to ghcr.io by CI (`b4bebf3ba486`, 2026-09-23)

## Later
- [ ] Drop the tvdb lookup for Sonarr once v5 (imdbId in Custom List) reaches the `develop` image
- [ ] Reopen Radarr#10787 / file the Sonarr equivalent citing the new `/lists` API
