#!/usr/bin/env bash
# Remove the old duplicate launchers (user-level only, no sudo):
#   - pinterest-wallpaper.service (systemd user unit)
#   - autostart .desktop entries that start Wally / the Pinterest wallpaper
#     script (including ones created via "Startup Applications")
# Prints what it removes. Safe to re-run.
set -uo pipefail

cfg="${XDG_CONFIG_HOME:-$HOME/.config}"

for unit in pinterest-wallpaper.service pinterest-wallpaper.timer; do
    systemctl --user disable --now "$unit" 2>/dev/null || true
    for f in "$cfg/systemd/user/$unit" "$cfg/systemd/user"/*.wants/"$unit"; do
        [ -e "$f" ] || [ -L "$f" ] && { echo "removing $f"; rm -f "$f"; }
    done
done
systemctl --user daemon-reload
systemctl --user reset-failed 2>/dev/null || true

shopt -s nullglob
for f in "$cfg"/autostart/*.desktop; do
    if grep -qiE 'pinterest-wallpaper|wally|wallpaper\.py' "$f"; then
        echo "removing $f"
        rm -f "$f"
    fi
done

for f in /etc/systemd/user/pinterest-wallpaper.* /etc/xdg/autostart/pinterest-wallpaper.desktop; do
    [ -e "$f" ] && echo "NOTE: $f is system-wide; remove it manually with sudo if you want it gone."
done
exit 0
