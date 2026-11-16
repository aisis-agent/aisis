#!/usr/bin/env bash
# Runs tests/*.mts against the overlay applied to the active quasar image's own sources, in scratch containers with no
# network (a mocked Home Assistant, no device actions, no sound). Nothing is deployed or restarted.
#
#   run_tests.sh [CONTAINER]        CONTAINER defaults to the active prod colour's quasar container
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
container=${1:-}
if [[ -z $container ]]; then
  active=$(python3 "$here/../../bluegreen/bgctl.py" get prod active)
  container=aisis-quasar-prod-$active
fi
image=$(docker inspect -f '{{.Config.Image}}' "$container")

scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
docker cp "$container:/src/." "$scratch/src" >/dev/null
python3 "$here/prepare_overlay.py" --baseline "$scratch/src" --output-dir "$scratch/overlay" >/dev/null
cp -a "$scratch/overlay/." "$scratch/src/"

status=0
for test in "$here"/tests/*.mts; do
  docker run --rm --network none --entrypoint /node_modules/.bin/tsx \
    -v "$scratch/src/backend:/src/backend:ro" -v "$scratch/src/routers:/src/routers:ro" \
    -v "$here/tests:/tests:ro" -w /tests "$image" "$(basename "$test")" || status=1
done
exit $status
