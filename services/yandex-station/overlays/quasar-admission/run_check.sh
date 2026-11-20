#!/usr/bin/env bash
# Builds the overlay from the ACTIVE colour's own sources (hash-checked), then in scratch containers of the image that colour RUNS, with
# no network: the image's eslint on the patched files, and check_admission.mts against the patched files (must pass). Then the same
# check against the image's UNPATCHED main.ts and router (only the new admission.ts is added so the check can load it): it must FAIL,
# or it proves nothing. Prints the overlay directory on the last line of stdout (nothing else goes to stdout); nothing is deployed,
# restarted or flipped.
#
#   run_check.sh [CONTAINER] [--router FILE]
#     CONTAINER defaults to the active prod colour's quasar container. --router takes routers/uniproxy.alice.yandex.net.ts from FILE
#     (the file the uniproxy-close-log overlay produced) instead of from the container.
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
container=
router=
while (($#)); do
  case $1 in
    --router) router=${2:?--router needs a file}; shift 2 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) container=$1; shift ;;
  esac
done
if [[ -z $container ]]; then
  active=$(python3 "$here/../../bluegreen/bgctl.py" get prod active)
  container=aisis-quasar-prod-$active
fi
image=$(docker inspect -f '{{.Image}}' "$container")

scratch=$(mktemp -d)
mkdir "$scratch/baseline"
docker cp "$container:/src/main.ts" "$scratch/baseline/main.ts" >/dev/null
mkdir "$scratch/baseline/routers"
docker cp "$container:/src/routers/uniproxy.alice.yandex.net.ts" "$scratch/baseline/routers/uniproxy.alice.yandex.net.ts" >/dev/null
prepare=("$here/prepare_overlay.py" --baseline "$scratch/baseline" --output-dir "$scratch/overlay")
if [[ -n $router ]]; then prepare+=(--router "$(realpath -- "$router")"); fi
python3 "${prepare[@]}" >&2
overlay=$scratch/overlay

patched=(-v "$overlay/admission.ts:/src/admission.ts:ro" -v "$overlay/main.ts:/src/main.ts:ro"
         -v "$overlay/routers/uniproxy.alice.yandex.net.ts:/src/routers/uniproxy.alice.yandex.net.ts:ro")
unpatched=(-v "$overlay/admission.ts:/src/admission.ts:ro")
check=(-v "$here/check_admission.mts:/src/routers/check_admission.mts:ro")

docker run --rm --network none -w / --entrypoint /node_modules/.bin/eslint "${patched[@]}" "$image" \
  src/admission.ts src/routers/uniproxy.alice.yandex.net.ts >&2
echo "eslint: clean (admission.ts, router)" >&2

echo "== with the overlay (must pass)" >&2
docker run --rm --network none --entrypoint /node_modules/.bin/tsx "${patched[@]}" "${check[@]}" "$image" \
  /src/routers/check_admission.mts 2>&1 | grep -v -i deprecat >&2
echo "== without the overlay (must fail: nothing in the image consults the policy)" >&2
if docker run --rm --network none --entrypoint /node_modules/.bin/tsx "${unpatched[@]}" "${check[@]}" "$image" \
    /src/routers/check_admission.mts >"$scratch/old.txt" 2>&1; then
  echo "the check passes without the overlay: it proves nothing" >&2
  exit 1
fi
grep -q '^ИТОГ ' "$scratch/old.txt" || { cat "$scratch/old.txt" >&2; echo "the run without the overlay crashed before its verdict" >&2; exit 1; }
grep -E '^(ИТОГ |FAIL )' "$scratch/old.txt" | cut -c1-170 >&2
echo "base image: $image" >&2
echo "$overlay"
