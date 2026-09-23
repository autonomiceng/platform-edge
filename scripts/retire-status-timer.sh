#!/bin/sh
# Upgrade step: retire the version 1 Edge status observer timer. Safe to rerun.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
name=platform-edge-status
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
if command -v systemctl >/dev/null 2>&1 &&
  systemctl --user disable --now "$name.timer" "$name.service" >/dev/null 2>&1; then
  echo "disabled and stopped $name.timer and $name.service"
fi
for file in "$units/timers.target.wants/$name.timer" "$units/$name.timer" "$units/$name.service" \
  "$root/data/status/bootstrap.json" "$root/data/console/.status.lock"; do
  if [ -e "$file" ] || [ -L "$file" ]; then
    rm -f "$file"
    echo "removed $file"
  fi
done
if rmdir "$root/data/status" 2>/dev/null; then echo "removed $root/data/status"; fi
if command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload >/dev/null 2>&1; then
  echo 'reloaded the systemd user manager'
fi
echo 'Status timer retired. Rerun python3 scripts/bootstrap.py to publish the version 2 Status Document.'
