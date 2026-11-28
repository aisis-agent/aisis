#!/usr/bin/env bash
# Blue/green deploy for the voice stack. See README.md.
#
#   deploy.sh deploy --image sha256:ID --corpus-receipt FILE [--no-speaker-wait]
#   deploy.sh deploy [--overlay SRC_DIR [--base IMAGE] | --context DIR] --corpus-receipt FILE [--tag TAG]
#   deploy.sh rollback
#   deploy.sh stage-up [IMAGE]      rebuild the stage stack (private DB copy) on IMAGE
#   deploy.sh stage-check           verified corpus evidence + API + handshake on each stage color and the switch
#   deploy.sh stage-down
#   deploy.sh adopt [CONTAINER]     one-time move of the running single quasar container into blue/green
#   deploy.sh unit NAME [FILE]      checked restart (with rollback) of adapter|stt|pm
#   deploy.sh status
#
# A deploy never touches the active color or the switch until the new image has
# passed the stage checks and the idle prod color has passed its own checks.
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
state_dir=${AISIS_BG_STATE:-$HOME/.local/share/aisis/bluegreen-state}
app_dir=${AISIS_CODE_DIR:-$HOME/xp/aisis/services/yandex-station/code}
code_dir_overridden=${AISIS_CODE_DIR:+1}
corpus_dir=${AISIS_CORPUS_DIR:-$HOME/.local/share/aisis/corpus}
runner_dir=$here/../corpus-runner   # the versioned corpus runner; the private corpus_dir only holds the data and dialog runner
corpus_receipt=${AISIS_CORPUS_RECEIPT:-}
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

# Corpus execution is isolated in native RunPod. Deployment consumes the actual
# native record and checks the current immutable image/source/mode at each color.
# No missing-evidence or service-failure path falls back to workstation tests.
corpus_preflight() {
  [[ -n $corpus_receipt ]] || die "give --corpus-receipt FILE or AISIS_CORPUS_RECEIPT"
  python3 "$runner_dir/offloaded_corpus.py" verify --receipt "$corpus_receipt" \
    --corpus-dir "$corpus_dir" --image "$1"
}

corpus_check() {
  [[ -n $corpus_receipt ]] || { log "corpus evidence missing; no local fallback"; return 1; }
  python3 "$runner_dir/offloaded_corpus.py" verify --receipt "$corpus_receipt" \
    --corpus-dir "$corpus_dir" --container "$1"
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
  local mode=context src="" base="" tag img="" active idle idle_up before
  tag=$(date +%Y%m%d-%H%M%S)
  local wait_speaker=1
  while (($#)); do
    case $1 in
      --overlay) mode=overlay; src=$2; shift 2 ;;
      --context) mode=context; src=$2; shift 2 ;;
      --base) base=$2; shift 2 ;;
      --image) img=$2; shift 2 ;;
      --corpus-receipt) corpus_receipt=$2; shift 2 ;;
      --tag) tag=$2; shift 2 ;;
      --no-speaker-wait) wait_speaker=0; shift ;;
      *) die "unknown option $1" ;;
    esac
  done
  [[ -n $corpus_receipt ]] || die "give --corpus-receipt FILE or AISIS_CORPUS_RECEIPT"
  if [[ -n $img ]]; then
    [[ -z $src && -z $base ]] || die "--image cannot be combined with build inputs"
    [[ $img =~ ^sha256:[0-9a-f]{64}$ ]] || die "--image requires the tested immutable image ID"
    corpus_preflight "$img" || die "candidate corpus evidence is invalid; no containers changed"
  else
    [[ -n $src ]] || die "give --image ID, --overlay SRC_DIR or --context DIR"
  fi
  maintenance_on
  ensure_state
  active=$(bg get prod active)
  idle=$([[ $active == blue ]] && echo green || echo blue)
  [[ -n $base ]] || base=$(bg get prod "IMG_$(upper "$active")")

  if [[ -z $img ]]; then
    log "[1/6] build ($mode, base $base)"
    img=$(build_image "$mode" "$src" "$base" "$tag")
    img=$(docker image inspect --format '{{.Id}}' "$img")
    corpus_preflight "$img" || die "built image differs from tested candidate; no containers changed"
  else
    log "[1/6] use the tested immutable candidate"
  fi
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
  img=$(docker inspect -f '{{.Image}}' "$legacy") || die "legacy container $legacy not found"
  corpus_preflight "$img" || die "adoption corpus evidence is invalid; no containers changed"
  maintenance_on
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

# Sibling modules of a unit's entry file that an update carries along when the file imports them: `unit` installs one file, so a
# module the new code newly depends on (or calls a new function of) would otherwise be missing or stale at the restart.
unit_companion_candidates() {
  case $1 in
    adapter) echo "announcement_cache.py hypothesis_store.py laya_route.py laya_quality.py classifier_journal.py asr_correct.py ha_client.py ha_call.py ha_ws.py ha_verify.py" ;;
    stt) echo "second_opinion.py spotter_clips.py voiceclips.py" ;;
  esac
}

# unit_imported_companions ENTRY_FILE MODULE.py...: those of the modules that ENTRY_FILE imports, directly or through another of the
# modules that sits next to it (the adapter imports ha_call, which imports ha_ws), one per line.
unit_imported_companions() {
  python3 - "$@" <<'PY'
import ast
import os
import sys
import tokenize


def imports(path):
    with tokenize.open(path) as source:
        tree = ast.parse(source.read())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


entry, candidates = sys.argv[1], sys.argv[2:]
folder = os.path.dirname(entry)
wanted, queue = [], [entry]
while queue:
    names = imports(queue.pop())
    for module in candidates:
        if module[:-3] in names and module not in wanted:
            wanted.append(module)
            if os.path.isfile(os.path.join(folder, module)):
                queue.append(os.path.join(folder, module))
print("\n".join(wanted))
PY
}

unit_invalidate_bytecode() {
  # Only the files of this update; preserve all other module caches.
  local stem=${1%.py}
  rm -f -- "$app_dir/$stem.pyc" "$app_dir"/__pycache__/"$stem".*.pyc
}

unit_install_file() {
  local source=$1 target=$app_dir/$2 mode=644
  [[ ! -f $target ]] || mode=$(stat -c %a "$target")
  install -m "$mode" "$source" "$target" || return 1
  unit_invalidate_bytecode "$2"
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
    unit_invalidate_bytecode "$file" || return 1
  done
}

# unit NAME [FILE]: install the entry file and the sibling modules it imports (unit_companion_candidates) together; restore all on failure.
cmd_unit() {
  local name=${1:?unit name} newfile=${2:-} file unit url backup_dir candidates imported module
  local -a companions=()
  case $name in
    adapter) file=adapter.py unit=aisis-station-adapter.service url=http://127.0.0.1:31117/healthz ;;
    stt)     file=stt_wyoming.py unit=aisis-station-stt.service url=http://127.0.0.1:31121/healthz ;;
    pm)      file=pm_cdp_server.py unit=aisis-station-pm.service url=http://127.0.0.1:31118/health ;;
    *) die "unknown unit $name (adapter|stt|pm)" ;;
  esac
  local -a files=("$file")
  candidates=$(unit_companion_candidates "$name")
  if [[ -n $newfile ]]; then
    python3 -m py_compile "$newfile" || die "$newfile does not compile"
    if [[ -n $candidates ]]; then
      # only the modules the new entry file imports, directly or through another candidate (adapter -> ha_call -> ha_ws)
      # shellcheck disable=SC2086  # plain module file names, split on purpose
      imported=$(unit_imported_companions "$newfile" $candidates) || die "cannot inspect $name dependencies"
      for module in $imported; do
        [[ -f $(dirname -- "$newfile")/$module ]] || die "$module must accompany the new $name"
        python3 -m py_compile "$(dirname -- "$newfile")/$module" || die "$module does not compile"
        companions+=("$module")
      done
      files+=("${companions[@]}")
    fi
  fi
  maintenance_on
  # Resolved only while the maintenance lock is held (the installer takes the same lock around drop-in install + daemon-reload),
  # so the unit cannot migrate between this lookup and the restart. Without an explicit AISIS_CODE_DIR the adapter's files go where
  # systemd actually runs it (adapter, stt or pm; its effective ExecStart: primary checkout or the deploy-main worktree), never where a directory
  # merely exists: a restart must run the code just installed.
  if [[ ( $name == adapter || $name == stt || $name == pm ) && -z $code_dir_overridden ]]; then
    local exec_start exec_path argv adapter_path
    exec_start=$(systemctl --user show -p ExecStart --value "$unit") || die "cannot read ExecStart of $unit"
    # systemd prints `{ path=P ; argv[]=P ARG... ; ignore_errors=... }` with no quoting: split on the field separators, not on
    # spaces, so a home directory containing spaces survives. argv[0] is the executable path P; the rest must be adapter.py alone.
    exec_path=${exec_start#*path=}; exec_path=${exec_path%% ; argv\[\]=*}
    argv=${exec_start#*argv\[\]=}; argv=${argv%% ; ignore_errors=*}
    [[ -n $exec_path && $argv == "$exec_path "* ]] || die "cannot parse the effective ExecStart of $unit; refusing to guess the install target"
    adapter_path=${argv#"$exec_path "}
    [[ $adapter_path == /*/$file ]] || die "$unit has no lone /…/$file argument in its effective ExecStart; refusing to guess the install target"
    app_dir=${adapter_path%/"$file"}
  fi
  [[ -f $app_dir/$file ]] || die "installed file missing: $file"
  mkdir -p "$state_dir/unit-backups"
  backup_dir=$(mktemp -d "$state_dir/unit-backups/$name.XXXXXX")
  for file in "${files[@]}"; do
    [[ ! -f $app_dir/$file ]] || cp -p "$app_dir/$file" "$backup_dir/$file"
  done
  for module in "${companions[@]}"; do
    if ! unit_install_file "$(dirname -- "$newfile")/$module" "$module"; then
      unit_restore_files "$backup_dir" "${files[@]}" \
        || die "restore failed; recover manually from $backup_dir"
      die "$module installation failed; previous files restored"
    fi
  done
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
