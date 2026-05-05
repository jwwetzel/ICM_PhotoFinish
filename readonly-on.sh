#!/usr/bin/env bash
# readonly-on.sh -- enable the read-only overlay filesystem and reboot.
# Use this AFTER a successful update to lock the SD card.
set -eu

echo "==> Enabling read-only overlay filesystem..."
if sudo raspi-config nonint enable_overlayfs 2>/dev/null; then
    echo "    Done (used: enable_overlayfs)."
else
    echo "    Falling back to do_overlayfs 0 ..."
    sudo raspi-config nonint do_overlayfs 0
fi

echo ""
echo "==> Rebooting in 5 seconds.  Ctrl+C to abort."
sleep 5
sudo reboot
