#!/usr/bin/env bash
# Builds the overlay from the active image's own sources (SHA-256 checked, no fuzz), then in scratch containers of that image with no
# network (stubs on loopback inside check_ha_intents_safety.mts; no device actions, no sound) runs the check WITH the overlay (must pass)
# and WITHOUT it (must fail: that is the old behaviour, or the check proves nothing). The patched routers/uniproxy/input-handler.ts is
# also loaded once, so a syntax or import slip in it shows here. Nothing is deployed, restarted or flipped; the container is only read.
#
#   run_check.sh [CONTAINER] [--ha-intents FILE [--grammar-hook FILE]]
#     CONTAINER defaults to the active prod colour's quasar container; the image checked is the one it RUNS (its image ID, not the tag it
#     was created from). --ha-intents takes backend/ha-intents.ts from FILE instead of the container: the output of another overlay
#     (anchor uniqueness only is checked then, see README.md). When that file imports a sibling the image does not have (the
#     correction-hook overlay's grammarNoop/grammarContextOpen), that overlay's grammar-hook.ts goes with it in --grammar-hook.
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
files=(backend/ha-intents.ts backend/turn-memory.ts backend/asr-correct.ts routers/uniproxy/input-handler.ts)
for file in "${files[@]}"; do
  mkdir -p "$scratch/src/$(dirname -- "$file")"
  docker cp "$container:/src/$file" "$scratch/src/$file" >/dev/null
done
args=(--src "$scratch/src" --output-dir "$scratch/overlay")
if [[ -n $input ]]; then args+=(--ha-intents "$input"); fi
python3 "$here/prepare_overlay.py" "${args[@]}" >/dev/null

# the files a run sees: the image's own unless a mount replaces them
run_check() {
  local mounts=(-v "$here/check_ha_intents_safety.mts:/src/backend/check_ha_intents_safety.mts:ro")
  if [[ -n $grammar_hook ]]; then mounts+=(-v "$(realpath -- "$grammar_hook"):/src/backend/grammar-hook.ts:ro"); fi
  if [[ $1 == with ]]; then
    for file in "${files[@]}" backend/negation.ts; do mounts+=(-v "$scratch/overlay/$file:/src/$file:ro"); done
  elif [[ -n $input ]]; then
    mounts+=(-v "$(realpath -- "$input"):/src/backend/ha-intents.ts:ro")
  fi
  docker run --rm --network none --entrypoint /node_modules/.bin/tsx "${mounts[@]}" "$image" /src/backend/check_ha_intents_safety.mts
}

echo "== with the overlay (must pass)"
run_check with
echo "== the patched input-handler.ts loads"
mounts=()
for file in "${files[@]}" backend/negation.ts; do mounts+=(-v "$scratch/overlay/$file:/src/$file:ro"); done
if [[ -n $grammar_hook ]]; then mounts+=(-v "$(realpath -- "$grammar_hook"):/src/backend/grammar-hook.ts:ro"); fi
docker run --rm --network none --entrypoint /node_modules/.bin/tsx "${mounts[@]}" "$image" \
  -e "import('/src/routers/uniproxy/input-handler.ts').then(m => console.log('input-handler.ts: InputHandler is a ' + typeof (m.InputHandler ?? m.default.InputHandler)))"
echo "== without the overlay (must fail: the old behaviour)"
if run_check without >"$scratch/old.txt" 2>&1; then
  echo "the check passes without the overlay: it proves nothing" >&2
  exit 1
fi
grep -q '^ИТОГ ' "$scratch/old.txt" || { cat "$scratch/old.txt"; echo "the run without the overlay crashed before its verdict" >&2; exit 1; }
grep '^ИТОГ ' "$scratch/old.txt"
