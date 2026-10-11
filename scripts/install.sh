#!/usr/bin/env bash
# Install Wally as a systemd *user* timer (no sudo). Safe to re-run.
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

command -v gallery-dl >/dev/null || echo "WARNING: gallery-dl not found on PATH (pip install --user -r requirements.txt)"
python3 -c 'import PIL' 2>/dev/null || echo "WARNING: Pillow missing (pip install --user -r requirements.txt)"

mkdir -p "$unit_dir"
sed "s|@PROJECT_DIR@|$project_dir|g" "$project_dir/systemd/wally.service" > "$unit_dir/wally.service"
cp "$project_dir/systemd/wally.timer" "$unit_dir/wally.timer"

systemctl --user daemon-reload
systemctl --user enable wally.timer
echo "Installed. It will run 60s after every login."
echo "Start it now without rebooting:  systemctl --user start wally.timer"
