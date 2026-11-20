#!/usr/bin/env bash
# Check out the pinned aisis-quasar source (services/yandex-station/quasar-source.lock) as a clean tree.
#
#   prepare-quasar-source.sh [DEST]   default DEST: $AISIS_QUASAR_SOURCE, else ~/.cache/aisis/quasar-source
#                                     (created, or fetched and re-checked-out when it already is a checkout of the
#                                     locked repository)
#
# Prints the absolute DEST on stdout. Refuses a tag that does not point at the locked commit and any tree that is not
# exactly that commit (untracked or ignored files included: a stray file must never reach a build context).
# Never touches a running container, a deploy state or a blue/green color.
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
lock=${AISIS_QUASAR_LOCK:-$here/../services/yandex-station/quasar-source.lock}
dest=${1:-${AISIS_QUASAR_SOURCE:-$HOME/.cache/aisis/quasar-source}}

die() { printf 'prepare-quasar-source: %s\n' "$*" >&2; exit 1; }
lock_value() { sed -n "s/^$1=//p" "$lock" | head -n 1; }

[[ -r $lock ]] || die "no lock file at $lock"
repo=$(lock_value repo); tag=$(lock_value tag); commit=$(lock_value commit)
[[ -n $repo && -n $tag && $commit =~ ^[0-9a-f]{40}$ ]] || die "lock needs repo, tag and a 40-hex commit"

if [[ -d $dest/.git ]]; then
  # The stored URL, not `remote get-url`: that one applies url.<base>.insteadOf rewriting.
  [[ $(git -C "$dest" config --get remote.origin.url) == "$repo" ]] || die "$dest is a checkout of another repository"
  git -C "$dest" fetch --quiet --tags origin
else
  [[ ! -e $dest ]] || die "$dest exists and is not a git checkout"
  git clone --quiet -- "$repo" "$dest"
fi
git -C "$dest" -c advice.detachedHead=false checkout --quiet "$commit"
[[ $(git -C "$dest" rev-parse "refs/tags/$tag^{commit}") == "$commit" ]] || die "tag $tag does not point at $commit"
[[ -z $(git -C "$dest" status --porcelain --ignored --untracked-files=all) ]] \
  || die "$dest is not a clean checkout of $commit (local, untracked or ignored files)"
(cd -- "$dest" && pwd)
