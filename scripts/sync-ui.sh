#!/bin/sh
# Vendor the canonical platform-ui kit (docker/console/platform.css) into sibling consoles, or
# check that their copies match.
#   scripts/sync-ui.sh <sibling-dir>...            copy, prefixed with a header naming the commit and checksum
#   scripts/sync-ui.sh --check [<sibling-dir>...]  exit 1 on drift; no dirs checks only the canonical file
# The copy goes to the first existing console directory of the sibling: docker/caddy/console
# (Gateway, Observability) or apps/web (Backplane).
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
canonical=$root/docker/console/platform.css
header_prefix='/* vendored from platform-edge@'
header_re='^/\* vendored from platform-edge@[0-9a-f]{7,40} sha256:[0-9a-f]{64} ; do not edit here \*/$'

usage() {
  echo 'usage: scripts/sync-ui.sh <sibling-dir>... | --check [<sibling-dir>...]' >&2
  exit 2
}
sha256() {
  if command -v sha256sum >/dev/null; then sha256sum | cut -d ' ' -f 1; else shasum -a 256 | cut -d ' ' -f 1; fi
}
console_dir() {
  for candidate in docker/caddy/console apps/web; do
    [ -d "$1/$candidate" ] && { echo "$1/$candidate"; return 0; }
  done
  return 1
}

mode=copy
if [ "${1-}" = '--check' ]; then
  mode=check
  shift
fi
[ "$mode" = check ] || [ "$#" -gt 0 ] || usage

# The canonical file carries its version and never a vendoring header.
case $(head -n 1 "$canonical") in
  "$header_prefix"*) echo "refused: $canonical starts with a vendoring header" >&2; exit 1 ;;
  '/* platform-ui v'*) ;;
  *) echo "refused: $canonical does not start with its platform-ui version line" >&2; exit 1 ;;
esac

failed=0
for dir in "$@"; do
  if ! target_dir=$(console_dir "$dir"); then
    echo "missing: no docker/caddy/console or apps/web in $dir" >&2; failed=1; continue
  fi
  target=$target_dir/platform.css
  if [ "$mode" = check ]; then
    if [ ! -f "$target" ]; then
      echo "missing: $target" >&2; failed=1; continue
    fi
    header=$(head -n 1 "$target")
    if ! printf '%s\n' "$header" | grep -qE "$header_re"; then
      echo "differs: $target has no valid vendoring header" >&2; failed=1; continue
    fi
    sha=$(printf '%s\n' "$header" | sed 's/^.*platform-edge@\([0-9a-f]*\) .*$/\1/')
    sum=$(printf '%s\n' "$header" | sed 's/^.*sha256:\([0-9a-f]*\) .*$/\1/')
    if [ "$(tail -n +2 "$target" | sha256)" != "$sum" ]; then
      echo "differs: $target does not match its header checksum" >&2; failed=1; continue
    fi
    if ! tail -n +2 "$target" | cmp -s - "$canonical"; then
      echo "differs: $target is not the current canonical kit" >&2; failed=1; continue
    fi
    # The header names the commit the copy came from; that commit must hold this content.
    if ! git -C "$root" show "$sha:docker/console/platform.css" 2>/dev/null | cmp -s - "$canonical"; then
      echo "differs: $target claims platform-edge@$sha, which is unknown here or had other content" >&2
      failed=1; continue
    fi
    echo "ok: $target (platform-edge@$sha)"
  else
    # A header naming a commit is only true when the canonical file is committed as is.
    if ! git -C "$root" show HEAD:docker/console/platform.css 2>/dev/null | cmp -s - "$canonical"; then
      echo "refused: $canonical has uncommitted changes; commit before vendoring" >&2; exit 1
    fi
    sha=$(git -C "$root" rev-parse --short HEAD)
    sum=$(sha256 < "$canonical")
    { printf '%s%s sha256:%s ; do not edit here */\n' "$header_prefix" "$sha" "$sum"; cat "$canonical"; } > "$target.tmp"
    mv -f "$target.tmp" "$target"
    echo "vendored: $target (platform-edge@$sha)"
  fi
done
exit "$failed"
