#!/bin/sh
# Vendor the canonical docs/conventions.md into sibling repos, or check that their copies match.
#   scripts/sync-conventions.sh <sibling-dir>...          copy, prefixed with a vendoring header
#   scripts/sync-conventions.sh --check [<sibling-dir>...] exit 1 on drift; no dirs checks only the canonical file
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
canonical=$root/docs/conventions.md
canonical_dir=$(CDPATH='' cd -- "$root/docs" && pwd -P)
header_prefix='<!-- vendored from platform-edge@'
header_re='^<!-- vendored from platform-edge@[0-9a-f]{7,40} ; do not edit here -->$'

usage() {
  echo 'usage: scripts/sync-conventions.sh <sibling-dir>... | --check [<sibling-dir>...]' >&2
  exit 2
}

mode=copy
if [ "${1-}" = '--check' ]; then
  mode=check
  shift
fi
[ "$mode" = check ] || [ "$#" -gt 0 ] || usage

# The canonical file never carries a header; one here means a sibling copy was pasted back.
case $(head -n 1 "$canonical") in
  "$header_prefix"*) echo "refused: $canonical starts with a vendoring header" >&2; exit 1 ;;
esac

failed=0
for dir in "$@"; do
  target=$dir/docs/conventions.md
  if [ "$mode" = check ]; then
    if [ ! -f "$target" ]; then
      echo "missing: $target" >&2; failed=1; continue
    fi
    if ! head -n 1 "$target" | grep -qE "$header_re"; then
      echo "differs: $target has no valid vendoring header" >&2; failed=1; continue
    fi
    if tail -n +2 "$target" | cmp -s - "$canonical"; then
      echo "ok: $target ($(head -n 1 "$target" | sed 's/^<!-- vendored from //; s/ ; do not edit here -->$//'))"
    else
      echo "differs: $target" >&2; failed=1
    fi
  else
    [ -d "$dir/docs" ] || { echo "missing: $dir/docs" >&2; exit 1; }
    if [ "$(CDPATH='' cd -- "$dir/docs" && pwd -P)" = "$canonical_dir" ]; then
      echo "refused: $dir is the canonical checkout" >&2; exit 1
    fi
    # A header naming a commit is only true when the canonical file is committed as is.
    if ! git -C "$root" diff --quiet HEAD -- docs/conventions.md; then
      echo "refused: $canonical has uncommitted changes; commit before vendoring" >&2; exit 1
    fi
    sha=$(git -C "$root" rev-parse --short HEAD)
    { printf '%s%s ; do not edit here -->\n' "$header_prefix" "$sha"; cat "$canonical"; } > "$target.tmp"
    mv -f "$target.tmp" "$target"
    echo "vendored: $target (platform-edge@$sha)"
  fi
done
exit "$failed"
