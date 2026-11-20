#!/usr/bin/env bash
# Builds the overlay from the ACTIVE colour's router source (hash-checked), then in scratch containers of the active image with no
# network: the image's own eslint on the patched file, and check_close_logging.mts (real WebSocket clients against the real router).
# Prints the overlay directory on the last line (stdout carries nothing else) for a candidate image built FROM the checked base (see
# README.md); nothing is deployed, restarted or flipped.
#
#   run_check.sh [CONTAINER]        CONTAINER defaults to the active prod colour's quasar container
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
container=${1:-}
if [[ -z $container ]]; then
  active=$(python3 "$here/../../bluegreen/bgctl.py" get prod active)
  container=aisis-quasar-prod-$active
fi
image=$(docker inspect -f '{{.Image}}' "$container")

scratch=$(mktemp -d)
docker cp "$container:/src/routers/uniproxy.alice.yandex.net.ts" "$scratch/baseline.ts" >/dev/null
python3 "$here/prepare_overlay.py" --baseline "$scratch/baseline.ts" --output-dir "$scratch/overlay" >/dev/null
patched=$scratch/overlay/routers/uniproxy.alice.yandex.net.ts

mount=(-v "$patched:/src/routers/uniproxy.alice.yandex.net.ts:ro")
docker run --rm --network none -w / --entrypoint /node_modules/.bin/eslint "${mount[@]}" "$image" src/routers/uniproxy.alice.yandex.net.ts >&2
echo "eslint: clean" >&2
docker run --rm --network none --entrypoint /node_modules/.bin/tsx "${mount[@]}" \
  -v "$here/check_close_logging.mts:/src/routers/check_close_logging.mts:ro" "$image" /src/routers/check_close_logging.mts >&2
echo "base image: $image" >&2
echo "$scratch/overlay"
