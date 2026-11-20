#!/usr/bin/env bash
# Builds the overlay from the active image's own ha-intents.ts and runs check_ha_verify_wording.mts in a scratch container of that
# image with no network (a stub ha-cli on loopback; no device actions, no sound). Nothing is deployed or restarted. Also runs the
# same check WITHOUT the overlay: it must fail (that is the old wording), or the check proves nothing.
#
#   run_check.sh [CONTAINER] [--ha-intents FILE [--grammar-hook FILE]]
#     CONTAINER defaults to the active prod colour's quasar container. The image checked is the one the container RUNS (its image
#     ID, not the tag it was created from). --ha-intents takes the input from FILE instead of the container: the file the
#     correction-hook overlay produces (it imports grammarNoop/grammarContextOpen, so that overlay's grammar-hook.ts goes with it
#     in --grammar-hook, mounted over the image's own).
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
container=
input=
grammar_hook=
while (($#)); do
  case $1 in
    --ha-intents) input=${2:?--ha-intents needs a file}; shift 2 ;;
    --grammar-hook) grammar_hook=${2:?--grammar-hook needs a file}; shift 2 ;;
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
trap 'rm -rf -- "$scratch"' EXIT
if [[ -n $input ]]; then
  cp -- "$input" "$scratch/ha-intents.ts"
else
  docker cp "$container:/src/backend/ha-intents.ts" "$scratch/ha-intents.ts" >/dev/null
fi
python3 "$here/prepare_overlay.py" --ha-intents "$scratch/ha-intents.ts" --output-dir "$scratch/overlay" >/dev/null

run_check() {
  local mounts=(-v "$here/check_ha_verify_wording.mts:/src/backend/check_ha_verify_wording.mts:ro" -v "$1:/src/backend/ha-intents.ts:ro")
  if [[ -n $grammar_hook ]]; then mounts+=(-v "$(realpath -- "$grammar_hook"):/src/backend/grammar-hook.ts:ro"); fi
  docker run --rm --network none --entrypoint /node_modules/.bin/tsx "${mounts[@]}" "$image" /src/backend/check_ha_verify_wording.mts
}

echo "== with the overlay (must pass)"
run_check "$scratch/overlay/backend/ha-intents.ts"
echo "== without the overlay (must fail: the old wording)"
if run_check "$scratch/ha-intents.ts" >"$scratch/old.txt" 2>&1; then
  echo "the check passes without the overlay: it proves nothing" >&2
  exit 1
fi
grep -q '^ИТОГ ' "$scratch/old.txt" || { cat "$scratch/old.txt"; echo "the run without the overlay crashed before its verdict" >&2; exit 1; }
grep '^ИТОГ ' "$scratch/old.txt"
