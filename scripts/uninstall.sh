#!/usr/bin/env bash
# Likk Large Valve Controller — Uninstaller
set -e

echo "Stopping and disabling valve service..."
sudo systemctl stop valve 2>/dev/null || true
sudo systemctl disable valve 2>/dev/null || true
sudo rm -f /etc/systemd/system/valve.service
sudo systemctl daemon-reload

echo "Removing udev rules..."
sudo rm -f /etc/udev/rules.d/99-gl7611.rules
sudo udevadm control --reload-rules

echo "Done. The install directory ~/likk-large-valve was NOT removed."
echo "To fully remove: rm -rf ~/likk-large-valve"
