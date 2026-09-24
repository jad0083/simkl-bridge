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

## CI/CD and testing
- [x] Stand-in Simkl (`tests/support/fake_simkl.py`) and `SIMKL_API_BASE`; integration tests run the real process over HTTP
- [x] End-to-end suite against real Sonarr/Radarr (stable, develop, URL base), on every change and weekly
- [x] Container smoke test and Trivy scan; image drops pip (its vendored packages were the only HIGH findings)
- [x] Soak with fault injection: 4 min per change, 60 min nightly
- [x] Release workflow: tag-verified, amd64 + arm64, `:latest`/`:X.Y.Z`/`:X.Y`, GitHub Release with generated notes; `main` publishes `:edge`; Dependabot
- [x] Branch protection on `main`: lint, tests (3.11–3.13), container, soak and the stable e2e legs required; develop-build e2e informational

## Later
- [ ] Drop the tvdb lookup for Sonarr once v5 (imdbId in Custom List) reaches the `develop` image
- [ ] Reopen Radarr#10787 / file the Sonarr equivalent citing the new `/lists` API
