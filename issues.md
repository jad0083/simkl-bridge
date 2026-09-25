# Issues

## Open
- [ ] The nightly soak (60 min, 20% Simkl faults, 20% lost app fetches) failed on its latency limit, not correctness: 176/176 edits delivered, but 6 in one window took 240-350 s against a 240 s limit that predated redelivery. Two consecutive failures (a lost fetch, then a Simkl fault on the re-request) legitimately cost two redelivery rounds. The limit is now derived from the design (watch interval + 3 redelivery rounds = 480 s) and the report adds p95 (2026-09-25)
- [ ] An accepted sync whose follow-up fetch failed was never retried, leaving the title to the app's own retry 6-12 h later (Sonarr/Radarr measure it from their last *successful* sync). Found by the CI soak at a 25% Simkl fault rate: one edit accepted, never delivered. Fixed in 0.3.1: delivery is confirmed by the app's own successful fetch (recognised by its User-Agent, so a user's Test or curl can't stand in for it), and unconfirmed syncs are requested again, at most five times (2026-09-24)
- [ ] The develop-build e2e leg failed on a registry TLS timeout pulling `lscr.io/linuxserver/sonarr:develop`, not on the bridge; the e2e runner now retries image pulls (2026-09-24)
- [x] A new Sonarr list waited for Sonarr's scheduler (up to its next 5-minute run): Sonarr syncs a list on edit, not on add, and the watcher treated first sight as a baseline. Found by the end-to-end suite; fixed by syncing each definition on first sight (released in 0.3.0) (2026-09-24)
- [x] `python -m simkl_bridge auth` crashed with a traceback when Simkl was unreachable; now a one-line error (released in 0.3.0) (2026-09-24)
- [x] The image carried pip, whose vendored msgpack and setuptools had fixable HIGH advisories; pip is now removed from the runtime image (released in 0.3.0) (2026-09-24)
- [ ] The watcher's startup log says `/sync/activities has no custom_lists.lists.all` when the field is present but `null`. Simkl leaves it `null` until the account's first list edit after the custom-list API went live. The watcher already handles it (hourly full checks until the stamp appears, then change-driven), but the message should say so rather than read like a fault (2026-09-23)
- [ ] Simkl custom-list API is **beta**; response shape may change. Tests pin the documented shape as of 2026-09-23, not a live capture (2026-09-23)
- [x] Not yet exercised against a real PRO token: the item/detail id field names come from the published OpenAPI examples (2026-09-23) — verified live 2026-09-23 against a VIP account: token refresh, `/lists/{id}` read and TVDB mapping all as documented
- [ ] Lists over 10,000 items (possible for `auto` lists) are refused with a 502: Simkl's API cannot page past that, and serving the first 10,000 would read as removals (2026-09-23)
- [ ] One service-wide lock serialises list builds; a first build of a large list with many missing ids blocks other lists for its duration. Acceptable at one-token scale; revisit if lists grow (2026-09-23)
- [ ] Sonarr 4.0.20 skips Simkl anime as "unsupported content type" in its native list (Sonarr#8978); this bridge sends tvdbId for anime, but whether Sonarr then adds them correctly as Anime series is unverified (2026-09-23)

## Fixed and released
- [x] A 200 list body without `items` was served as `[]`, which an arr cleaning its library reads as removals; list reads now fail unless provably complete (review finding, 2026-09-23)
- [x] Silent page clamping past 10,000 items or a mid-read edit could serve duplicated or missing items; page number, `updated_at` and item count are now checked (2026-09-23)
- [x] An unknown Simkl id (`200 []` from the catalog) failed every sync, and a detail `404` was reported as "list not found"; both are now "no mapping" (2026-09-23)
- [x] A list with no `updated_at`, or an `auto` list, could stay stale forever; the gate is skipped for them and every list is fully re-read daily (2026-09-23)
- [x] Films with a TMDb id but no IMDb id were sent imdb-less, which Radarr's StevenLu path resolves by title search; Radarr now gets TMDb-keyed entries only (2026-09-23)
- [x] Pacing sat exactly at Simkl's 10 GET/s with no retry; now 0.12 s with 429/5xx backoff, and network errors are a 502 rather than a 500 (2026-09-23)
- [x] Every item triggered a catalog lookup although list items already carry tvdb/tmdb; the earlier design doc wrongly said they did not. The id cache was also rewritten per lookup; now flushed once per build (2026-09-23)
- [x] Hardening: saved access token tied to its grant, `invalidate` ignores already-replaced tokens, missing `expires_in` no longer refreshes every call, redirects are not followed (would forward `Authorization`), device-flow polling survives 429/5xx, CI `packages: write` scoped to the image job (2026-09-23)
- [x] Memoised per-list feeds never re-checked a missing id mapping; feeds are now rebuilt per request from the id cache (caught by `test_a_missing_mapping_is_retried_after_a_week`, 2026-09-23)
