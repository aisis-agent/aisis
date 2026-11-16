#!/usr/bin/env bash
# Blue/green deploy for the voice stack. See README.md.
#
#   deploy.sh deploy [--overlay SRC_DIR [--base IMAGE] | --context DIR] [--tag TAG] [--no-speaker-wait]
#   deploy.sh rollback
#   deploy.sh stage-up [IMAGE]      rebuild the stage stack (private DB copy) on IMAGE
#   deploy.sh stage-check           corpus + API + uniproxy handshake on both stage colors and the stage switch
#   deploy.sh stage-down
#   deploy.sh adopt [CONTAINER]     one-time move of the running single quasar container into blue/green
#   deploy.sh unit NAME [FILE]      checked restart (with rollback) of adapter|stt|ha-cli|pm
#   deploy.sh status
#
# A deploy never touches the active color or the switch until the new image has
# passed the stage checks and the idle prod color has passed its own checks.
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
state_dir=${AISIS_BG_STATE:-$HOME/.local/share/aisis/bluegreen-state}
app_dir=${AISIS_CODE_DIR:-$HOME/xp/aisis/services/yandex-station/code}
corpus_dir=${AISIS_CORPUS_DIR:-$HOME/.local/share/aisis/corpus}
postgres_container=${AISIS_POSTGRES_CONTAINER:-aisis-yandex-postgres-1}
speaker_wait=${AISIS_SPEAKER_WAIT_S:-90}

bg() { python3 "$here/bgctl.py" "$@"; }
log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die() { log "ABORT: $*"; exit 1; }
upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }

# Tells failover.py not to act while a deploy deliberately restarts things.
maintenance_off() {
  if [[ -f $state_dir/maintenance && $(<"$state_dir/maintenance") == "$maintenance_owner" ]]; then
    rm -f -- "$state_dir/maintenance"
  fi
}

maintenance_on() {
  local previous temporary
  mkdir -p "$state_dir"
  # flock ownership ends with the process, including SIGKILL or a host reboot.
  exec {maintenance_lock_fd}>"$state_dir/maintenance.lock"
  flock -n "$maintenance_lock_fd" || die "another deployment holds the process lock"
  if [[ -e $state_dir/maintenance ]]; then
    previous=$(<"$state_dir/maintenance")
    # Only this protocol's abandoned markers are reclaimable. Unknown operator or
    # legacy maintenance remains protected regardless of the file's age.
    [[ $previous =~ ^aisis-deployment-lock-v1:[0-9]+:\.maintenance\.[a-zA-Z0-9]+$ ]] \
      || die "unrecognized maintenance owner; preserve it for operator coordination"
  fi
  temporary=$(mktemp "$state_dir/.maintenance.XXXXXX")
  maintenance_owner="aisis-deployment-lock-v1:$BASHPID:${temporary##*/}"
  printf '%s\n' "$maintenance_owner" >"$temporary"
  mv -- "$temporary" "$state_dir/maintenance"
  trap maintenance_off EXIT
}
maintenance_touch() {
  # Refresh the owned inode only. A concurrent replacement cannot be touched or
  # recreated, even between the owner check and the timestamp update.
  if ! python3 - "$state_dir/maintenance" "$maintenance_owner" <<'PY'
import os
import stat
import sys
path, owner = sys.argv[1:]
try:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
except OSError:
    raise SystemExit(1) from None
try:
    inode = os.fstat(descriptor)
    if not stat.S_ISREG(inode.st_mode) or os.read(descriptor, 4096) != (owner + "\n").encode():
        raise SystemExit(1)
    if not os.path.samestat(inode, os.stat(path, follow_symlinks=False)):
        raise SystemExit(1)
    os.utime(descriptor, None)
    if not os.path.samestat(inode, os.stat(path, follow_symlinks=False)):
        raise SystemExit(1)
except OSError:
    raise SystemExit(1) from None
finally:
    os.close(descriptor)
PY
  then
    die "maintenance ownership changed; preserve the marker and coordinate"
  fi
}

ensure_state() {
  [[ -f "$state_dir/state.env" ]] && return 0
  local img
  img=$(docker inspect -f '{{.Config.Image}}' "${LEGACY_CONTAINER:-aisis-yandex-quasar-1}" 2>/dev/null) \
    || die "no state and no legacy container to take an image from; run: bgctl.py init IMAGE"
  bg init "$img"
}

# corpus_check CONTAINER: the intent corpus must pass 100% and cover every corpus line.
corpus_check() {
  local c=$1 out line pass total expected
  expected=$(wc -l <"$corpus_dir/intents-ru.jsonl")
  docker exec "$c" mkdir -p /src/backend/corpus
  docker cp "$corpus_dir/." "$c:/src/backend/corpus/"
  out=$(docker exec -e GRAMMAR_HOOK=off -w /src/backend/corpus "$c" /node_modules/.bin/tsx run_corpus.mts 2>&1) || true
  line=$(grep -a 'ИТОГ' <<<"$out" || true)
  [[ $line =~ ([0-9]+)/([0-9]+) ]] || { log "corpus on $c: no result line"; return 1; }
  pass=${BASH_REMATCH[1]} total=${BASH_REMATCH[2]}
  log "corpus on $c: $pass/$total (expected $expected)"
  [[ $pass == "$total" && $total == "$expected" ]] || return 1
  # Multi-turn dialogs against a mocked HA: confirmations, slot fill, earcon, volume directive.
  out=$(docker exec -e GRAMMAR_HOOK=off -w /src/backend/corpus "$c" /node_modules/.bin/tsx run_dialogs.mts 2>&1) || true
  line=$(grep -a 'ДИАЛОГИ' <<<"$out" || true)
  [[ $line =~ ([0-9]+)/([0-9]+) ]] || { log "dialogs on $c: no result line"; return 1; }
  log "dialogs on $c: ${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
  [[ ${BASH_REMATCH[1]} == "${BASH_REMATCH[2]}" ]]
}

build_image() {
  local mode=$1 src=$2 base=$3 tag=$4 img="aisis-quasar:bg-$4" ctx
  if [[ $mode == overlay ]]; then
    [[ -d $src ]] || die "overlay dir not found: $src"
    ctx=$(mktemp -d)
    cp -a "$src" "$ctx/src"
    printf 'FROM %s\nCOPY src /src\n' "$base" >"$ctx/Dockerfile"
    docker build -q -t "$img" "$ctx" >/dev/null || { rm -rf "$ctx"; die "image build failed"; }
    rm -rf "$ctx"
  else
    docker build -q -t "$img" "$src" >/dev/null || die "image build failed"
  fi
  printf '%s' "$img"
}

stage_db_reset() {
  log "stage: copying prod database into quasar_stage"
  docker exec "$postgres_container" psql -q -U quasar -d postgres -v ON_ERROR_STOP=1 \
    -c 'DROP DATABASE IF EXISTS quasar_stage WITH (FORCE)' -c 'CREATE DATABASE quasar_stage' >/dev/null
  docker exec "$postgres_container" sh -c 'pg_dump -U quasar quasar | psql -q -U quasar -d quasar_stage -v ON_ERROR_STOP=1 >/dev/null'
}

stage_up() {
  local img=${1:-} color
  ensure_state
  [[ -n $img ]] || img=$(bg get prod "IMG_$(upper "$(bg get prod active)")")
  log "stage: image $img"
  bg compose stage rm -sf quasar-stage-blue quasar-stage-green >/dev/null 2>&1 || true
  stage_db_reset
  for color in blue green; do bg set-image stage "$color" "$img"; done
  bg render stage
  bg compose stage up -d --force-recreate >/dev/null 2>&1 || die "stage compose up failed"
  for color in blue green; do
    bg wait stage "$color" 90 || die "stage $color did not become healthy (docker logs aisis-quasar-stage-$color)"
  done
}

stage_check() {
  local color
  for color in blue green; do
    corpus_check "aisis-quasar-stage-$color" || return 1
    bg smoke stage "$color" || return 1
  done
  bg smoke stage switch || return 1
}

cmd_stage_up() { maintenance_on; stage_up "${1:-}"; log "stage is up"; }
cmd_stage_check() { stage_check && log "stage checks passed"; }
cmd_stage_down() { bg compose stage down --remove-orphans; }

# Drop old bg-* images after a successful flip: keep both prod colors' images
# (active + rollback) and anything a container still references.
prune_old_images() {
  local keep used t
  keep=" $(bg get prod IMG_BLUE) $(bg get prod IMG_GREEN) "
  used=" $(docker ps -a --format '{{.Image}}' | sort -u | tr '\n' ' ') "
  for t in $(docker images aisis-quasar --format '{{.Repository}}:{{.Tag}}' | grep ':bg-'); do
    [[ $keep == *" $t "* || $used == *" $t "* ]] && continue
    docker rmi "$t" >/dev/null 2>&1 && log "pruned old image $t"
  done
}

cmd_deploy() {
  local mode=context src="" base="" tag img active idle idle_up before
  tag=$(date +%Y%m%d-%H%M%S)
  local wait_speaker=1
  while (($#)); do
    case $1 in
      --overlay) mode=overlay; src=$2; shift 2 ;;
      --context) mode=context; src=$2; shift 2 ;;
      --base) base=$2; shift 2 ;;
      --tag) tag=$2; shift 2 ;;
      --no-speaker-wait) wait_speaker=0; shift ;;
      *) die "unknown option $1" ;;
    esac
  done
  [[ -n $src ]] || die "give --overlay SRC_DIR or --context DIR"
  maintenance_on
  ensure_state
  active=$(bg get prod active)
  idle=$([[ $active == blue ]] && echo green || echo blue)
  [[ -n $base ]] || base=$(bg get prod "IMG_$(upper "$active")")

  log "[1/6] build ($mode, base $base)"
  img=$(build_image "$mode" "$src" "$base" "$tag")
  log "image: $img"

  log "[2/6] stage: new image, private DB copy, no speaker, HA dry-run"
  stage_up "$img"
  maintenance_touch
  stage_check || die "stage checks failed on $img; prod untouched"

  log "[3/6] prod idle color: $idle"
  local prev_img
  prev_img=$(bg get prod "IMG_$(upper "$idle")")
  bg set-image prod "$idle" "$img"
  bg compose prod up -d --no-deps --force-recreate "quasar-prod-$idle" >/dev/null 2>&1 \
    || { bg set-image prod "$idle" "$prev_img"; die "could not start prod $idle"; }
  if ! bg wait prod "$idle" 90 || ! corpus_check "aisis-quasar-prod-$idle" || ! bg smoke prod "$idle"; then
    log "idle $idle failed its checks; restoring previous image on it"
    bg set-image prod "$idle" "$prev_img"
    bg compose prod up -d --no-deps --force-recreate "quasar-prod-$idle" >/dev/null 2>&1 || true
    die "prod $idle failed pre-flip checks; $active still serving"
  fi
  maintenance_touch

  before=$(bg devices prod || true)
  log "[4/6] flip $active -> $idle (connected before: ${before:-none})"
  bg flip prod "$idle" >&2

  log "[5/6] verify through the switch"
  if ! bg smoke prod switch >/dev/null; then
    bg flip prod "$active" >&2 || true
    die "post-flip smoke failed; rolled back to $active"
  fi
  if [[ $wait_speaker == 1 && -n $before ]]; then
    local waited=0
    until [[ -n $(bg devices prod || true) ]]; do
      ((waited++ >= speaker_wait)) && { bg flip prod "$active" >&2 || true; die "speaker did not reconnect to $idle within ${speaker_wait}s; rolled back to $active"; }
      sleep 1
    done
    log "speaker reconnected on $idle after ~${waited}s"
  fi

  log "[6/6] done: $idle is active on $img; $active stays warm for rollback (image $(bg get prod "IMG_$(upper "$active")"))"
  prune_old_images || true
}

cmd_rollback() {
  local active standby
  maintenance_on
  active=$(bg get prod active); standby=$(bg get prod standby)
  bg health prod "$standby" >/dev/null || die "standby $standby is not healthy; not flipping"
  bg flip prod "$standby" >&2
  bg smoke prod switch >/dev/null || { bg flip prod "$active" >&2; die "post-rollback smoke failed; flipped back"; }
  log "rolled back: $standby is active"
}

# adopt: move the single legacy container onto blue/green. Downtime is only the gap
# between stopping the legacy container and the switch binding the canonical ports.
cmd_adopt() {
  local legacy=${1:-aisis-yandex-quasar-1} img before
  maintenance_on
  img=$(docker inspect -f '{{.Config.Image}}' "$legacy") || die "legacy container $legacy not found"
  bg init "$img" --force
  bg render prod
  log "starting blue and green on $img next to the legacy container"
  bg compose prod up -d quasar-prod-blue quasar-prod-green >/dev/null 2>&1 || die "colors failed to start"
  for color in blue green; do
    bg wait prod "$color" 90 || die "$color unhealthy"
    corpus_check "aisis-quasar-prod-$color" || die "corpus failed on $color"
    bg smoke prod "$color" || die "smoke failed on $color"
  done
  before=$(curl -s -m3 http://127.0.0.1:31116/devices | grep -c '"connected":true' || true)
  log "cutover: legacy container -> switch (speaker connected before: $before)"
  docker update --restart=no "$legacy" >/dev/null
  docker stop -t 2 "$legacy" >/dev/null
  bg compose prod up -d switch-prod >/dev/null 2>&1 || {
    log "switch failed to start; reviving legacy container"; docker update --restart=unless-stopped "$legacy" >/dev/null; docker start "$legacy" >/dev/null
    die "switch failed"; }
  bg smoke prod switch >/dev/null || die "switch smoke failed (legacy container $legacy kept stopped for manual revive)"
  if ((before > 0)); then
    local waited=0
    until [[ -n $(bg devices prod || true) ]]; do
      ((waited++ >= speaker_wait)) && die "speaker has not reconnected after ${speaker_wait}s"
      sleep 1
    done
    log "speaker reconnected on blue after ~${waited}s"
  fi
  log "adopted; legacy container $legacy is stopped and kept (docker rm $legacy when no longer needed)"
}

adapter_needs_cache() {
  python3 - "$1" <<'PY'
import ast
import sys
import tokenize
with tokenize.open(sys.argv[1]) as source:
    tree = ast.parse(source.read())
needed = any(
    (isinstance(node, ast.Import) and any(item.name == "announcement_cache" for item in node.names))
    or (isinstance(node, ast.ImportFrom) and node.module == "announcement_cache")
    for node in ast.walk(tree)
)
print("required" if needed else "none")
PY
}

unit_invalidate_cache_bytecode() {
  # Only the companion belongs to this update; preserve all other module caches.
  rm -f -- "$app_dir/announcement_cache.pyc" "$app_dir"/__pycache__/announcement_cache.*.pyc
}

unit_install_file() {
  local source=$1 target=$app_dir/$2 mode=644
  [[ ! -f $target ]] || mode=$(stat -c %a "$target")
  install -m "$mode" "$source" "$target" || return 1
  if [[ $2 == announcement_cache.py ]]; then unit_invalidate_cache_bytecode; fi
}

unit_restore_files() {
  local backup_dir=$1 file
  shift
  for file in "$@"; do
    if [[ -f $backup_dir/$file ]]; then
      install -m "$(stat -c %a "$backup_dir/$file")" "$backup_dir/$file" "$app_dir/$file" || return 1
    else
      rm -f -- "$app_dir/$file" || return 1
    fi
    if [[ $file == announcement_cache.py ]]; then unit_invalidate_cache_bytecode || return 1; fi
  done
}

# unit NAME [FILE]: install adapter and its cache module together; restore both on failure.
cmd_unit() {
  local name=${1:?unit name} newfile=${2:-} file unit url cache_source="" backup_dir cache_needed
  case $name in
    adapter) file=adapter.py unit=aisis-station-adapter.service url=http://127.0.0.1:31117/healthz ;;
    stt)     file=stt_wyoming.py unit=aisis-station-stt.service url=http://127.0.0.1:31121/healthz ;;
    ha-cli)  file=hacli.py unit=aisis-ha-cli.service url=http://127.0.0.1:8795/health ;;
    pm)      file=pm_cdp_server.py unit=aisis-station-pm.service url=http://127.0.0.1:31118/health ;;
    *) die "unknown unit $name (adapter|stt|ha-cli|pm)" ;;
  esac
  local -a files=("$file")
  [[ -f $app_dir/$file ]] || die "installed file missing: $file"
  if [[ -n $newfile ]]; then
    python3 -m py_compile "$newfile" || die "$newfile does not compile"
    if [[ $name == adapter ]]; then
      cache_needed=$(adapter_needs_cache "$newfile") || die "cannot inspect adapter dependencies"
      if [[ $cache_needed == required ]]; then
        cache_source=$(dirname -- "$newfile")/announcement_cache.py
        [[ -f $cache_source ]] || die "announcement_cache.py must accompany the new adapter"
        python3 -m py_compile "$cache_source" || die "announcement_cache.py does not compile"
        files+=(announcement_cache.py)
      fi
    fi
  fi
  maintenance_on
  mkdir -p "$state_dir/unit-backups"
  backup_dir=$(mktemp -d "$state_dir/unit-backups/$name.XXXXXX")
  for file in "${files[@]}"; do
    [[ ! -f $app_dir/$file ]] || cp -p "$app_dir/$file" "$backup_dir/$file"
  done
  if [[ -n $cache_source ]] && ! unit_install_file "$cache_source" announcement_cache.py; then
    unit_restore_files "$backup_dir" "${files[@]}" \
      || die "restore failed; recover manually from $backup_dir"
    die "cache module installation failed; previous files restored"
  fi
  if [[ -n $newfile ]] && ! unit_install_file "$newfile" "${files[0]}"; then
    unit_restore_files "$backup_dir" "${files[@]}" \
      || die "restore failed; recover manually from $backup_dir"
    die "unit installation failed; previous files restored"
  fi
  local i
  if systemctl --user restart "$unit"; then
    for i in $(seq 1 30); do
      if curl -fsS -m2 "$url" >/dev/null 2>&1; then log "$name healthy after ${i}s"; return 0; fi
      sleep 1
    done
  fi
  log "$name unhealthy after restart; restoring $backup_dir"
  unit_restore_files "$backup_dir" "${files[@]}" \
    || die "restore failed; recover manually from $backup_dir"
  systemctl --user restart "$unit" \
    || die "$name previous files restored but restart failed; recover from $backup_dir"
  die "$name rolled back to the previous files"
}


cmd_status() { bg status; }

cmd=${1:-}
shift || true
case $cmd in
  deploy) cmd_deploy "$@" ;;
  rollback) cmd_rollback ;;
  stage-up) cmd_stage_up "$@" ;;
  stage-check) cmd_stage_check ;;
  stage-down) cmd_stage_down ;;
  adopt) cmd_adopt "$@" ;;
  unit) cmd_unit "$@" ;;
  status) cmd_status ;;
  *) sed -n '2,15p' "${BASH_SOURCE[0]}" >&2; exit 2 ;;
esac
