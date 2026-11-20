#!/usr/bin/env bash
# Builds the overlay from the ACTIVE colour's three sources (hash-checked), then in scratch containers of the active image with no
# network runs the two checks WITH the overlay (they must pass) and WITHOUT it (they must fail: the red proof that they test the bug).
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
mkdir -p "$scratch/baseline/backend" "$scratch/baseline/codecs"
for file in backend/fuzzy-ru.ts codecs/opus-processor.ts codecs/ogg-stitcher.ts; do
  docker cp "$container:/src/$file" "$scratch/baseline/$file" >/dev/null
done
python3 "$here/prepare_overlay.py" --baseline-dir "$scratch/baseline" --output-dir "$scratch/overlay" >/dev/null

run_checks() {  # run_checks LABEL [OVERLAY_DIR]: prints one line per check, returns the number of failing checks
  local label=$1 overlay=${2:-} failed=0 mounts=() check
  if [[ -n $overlay ]]; then
    for file in backend/fuzzy-ru.ts codecs/opus-processor.ts codecs/ogg-stitcher.ts; do
      mounts+=(-v "$overlay/$file:/src/$file:ro")
    done
  fi
  for check in backend/check_fuzzy_bounds.mts codecs/check_codecs.mts; do
    if docker run --rm --network none --memory 6g --entrypoint /node_modules/.bin/tsx "${mounts[@]}" \
      -v "$here/$(basename "$check"):/src/$check:ro" "$image" "/src/$check" >"$scratch/$(basename "$check").$label.log" 2>&1; then
      echo "$label: $(basename "$check") passed" >&2
    else
      echo "$label: $(basename "$check") FAILED" >&2
      failed=$((failed + 1))
    fi
  done
  return "$failed"
}

run_checks overlay "$scratch/overlay" || { echo "the checks must pass with the overlay; logs in $scratch" >&2; exit 1; }
red=0
run_checks baseline || red=$?
if [[ $red -ne 2 ]]; then
  echo "$red of 2 checks failed WITHOUT the overlay: each must fail, or it does not test its bug; logs in $scratch" >&2
  exit 1
fi
echo "baseline: both checks failed as they must" >&2
echo "base image: $image" >&2
echo "$scratch/overlay"
