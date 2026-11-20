#!/usr/bin/env bash
# Applies the overlay to the ACTIVE quasar image's own ha-intents.ts and runs check_ac_defer.mts in a scratch container of that
# image with no network (a mocked Home Assistant, no device actions, no sound). Nothing is deployed or restarted.
#
#   run_check.sh [CONTAINER] [--baseline-only]   CONTAINER defaults to the active prod colour's quasar container;
#                                                --baseline-only runs the check against the UNPATCHED file (it must fail: the incident)
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
container=""
baseline_only=0
for arg in "$@"; do
  case $arg in
    --baseline-only) baseline_only=1 ;;
    *) container=$arg ;;
  esac
done
if [[ -z $container ]]; then
  active=$(python3 "$here/../../bluegreen/bgctl.py" get prod active)
  container=aisis-quasar-prod-$active
fi
image=$(docker inspect -f '{{.Image}}' "$container")  # the immutable id the container runs, not a tag that may have moved

scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
mkdir -p "$scratch/backend"
docker cp "$container:/src/backend/ha-intents.ts" "$scratch/backend/ha-intents.ts" >/dev/null
if [[ $baseline_only -eq 0 ]]; then
  python3 "$here/prepare_overlay.py" --baseline "$scratch/backend/ha-intents.ts" --output-dir "$scratch/overlay" >/dev/null
  cp "$scratch/overlay/backend/ha-intents.ts" "$scratch/backend/ha-intents.ts"
fi

docker run --rm --network none --entrypoint /node_modules/.bin/tsx \
  -v "$scratch/backend/ha-intents.ts:/src/backend/ha-intents.ts:ro" \
  -v "$here/check_ac_defer.mts:/src/backend/check_ac_defer.mts:ro" \
  -v "$here/phrases.json:/src/backend/ac-phrases.json:ro" \
  "$image" /src/backend/check_ac_defer.mts
