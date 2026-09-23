#!/bin/sh
# Upgrade step: retire the version 1 Edge status observer timer. Safe to rerun.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
name=platform-edge-status
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
installed=''
for unit in "$name.timer" "$name.service"; do
  if [ -e "$units/$unit" ]; then installed="$installed $unit"; fi
done
# A timer left running would keep publishing version 1 status over the new document.
if [ -n "$installed" ] && command -v systemctl >/dev/null 2>&1; then
  # shellcheck disable=SC2086 # installed holds unit names without spaces
  systemctl --user disable --now $installed ||
    { echo "cannot stop$installed; rerun as the user who installed the timer" >&2; exit 1; }
  echo "disabled and stopped$installed"
fi
for file in "$units/timers.target.wants/$name.timer" "$units/$name.timer" "$units/$name.service" \
  "$root/data/status/bootstrap.json" "$root/data/console/.status.lock"; do
  if [ -e "$file" ] || [ -L "$file" ]; then
    rm -f "$file"
    echo "removed $file"
  fi
done
if rmdir "$root/data/status" 2>/dev/null; then echo "removed $root/data/status"; fi
if [ -n "$installed" ] && command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload
  echo 'reloaded the systemd user manager'
fi
echo 'Status timer retired. Rerun python3 scripts/bootstrap.py to publish the version 2 Status Document.'
