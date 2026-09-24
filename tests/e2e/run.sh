#!/usr/bin/env bash
# Run the end-to-end suite locally or in CI.
#
#   tests/e2e/run.sh                              # Sonarr/Radarr :latest, no URL base
#   SONARR_TAG=develop RADARR_TAG=develop tests/e2e/run.sh
#   URL_BASE=/arr tests/e2e/run.sh                # apps served under a URL base
#
# Needs Docker and internet access: Sonarr and Radarr look titles up on their
# own metadata services when adding them.
set -euo pipefail
cd "$(dirname "$0")"

export E2E_DIR; E2E_DIR=$(mktemp -d)
export URL_BASE="${URL_BASE:-}" SONARR_TAG="${SONARR_TAG:-latest}" RADARR_TAG="${RADARR_TAG:-latest}"
trap 'status=$?; if [ $status -ne 0 ]; then docker compose logs --no-color --tail=200 >&2 || true; fi;
      docker compose down -v --remove-orphans >/dev/null 2>&1 || true;
      docker run --rm -v "$E2E_DIR:/w" alpine sh -c "rm -rf /w/*" >/dev/null 2>&1 || true;
      rmdir "$E2E_DIR" 2>/dev/null || true; exit $status' EXIT

# Pre-seed each app's config.xml so its API key and URL base are known.
for app in sonarr:8989 radarr:7878; do
  name=${app%%:*}; port=${app##*:}
  mkdir -p "$E2E_DIR/$name"
  cat > "$E2E_DIR/$name/config.xml" <<XML
<Config>
  <BindAddress>*</BindAddress>
  <Port>$port</Port>
  <UrlBase>$URL_BASE</UrlBase>
  <ApiKey>e2e0${name}0key000000000000000000</ApiKey>
  <AuthenticationMethod>External</AuthenticationMethod>
  <AuthenticationRequired>DisabledForLocalAddresses</AuthenticationRequired>
  <LogLevel>info</LogLevel>
  <UpdateMechanism>Docker</UpdateMechanism>
</Config>
XML
done
mkdir -p "$E2E_DIR/tv" "$E2E_DIR/movies"
chmod -R a+rwX "$E2E_DIR"

# Registries (lscr.io, ghcr.io) have transient TLS and timeout failures;
# an image that can't be fetched is not a test result, so pulls are retried.
for attempt in 1 2 3 4; do
  docker compose pull --quiet --ignore-buildable && break
  [ "$attempt" = 4 ] && { echo "image pull failed 4 times" >&2; exit 1; }
  sleep $((attempt * 15))
done
docker compose up -d --build
"${PYTHON:-python3}" -m pytest -q -p no:cacheprovider -o addopts="" -s test_e2e.py "$@"
