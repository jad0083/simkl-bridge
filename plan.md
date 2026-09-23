# Plan

## Bridge
- [ ] Serve Simkl custom lists to Sonarr (`/sonarr/{id}`, tvdbId) and Radarr (`/radarr/{id}`, tmdb id + imdb_id)
- [ ] AUTH V2 token ownership: refresh 1 day before the 7-day expiry or on 401, persisted 0600
- [ ] Device-flow bootstrap (`python -m simkl_bridge auth`) writing the refresh token to a 0600 file, never to screen
- [ ] Id lookups via the cached catalog endpoints, persisted; missing mappings re-checked weekly
- [ ] List reads gated on `updated_at`; never serve `[]` on an error
- [ ] Anime lists split: series/OVA/ONA/special → Sonarr, films → Radarr
- [ ] Container image (stdlib only, non-root, healthcheck) built by CI to ghcr.io/jad0083/simkl-bridge

## Release
- [ ] Public repo `jad0083/simkl-bridge` and first image published to ghcr.io by CI

## Later
- [ ] Drop the tvdb lookup for Sonarr once v5 (imdbId in Custom List) reaches the `develop` image
- [ ] Reopen Radarr#10787 / file the Sonarr equivalent citing the new `/lists` API
