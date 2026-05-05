#!/usr/bin/env bash
# readonly-off.sh -- disable the read-only overlay filesystem and reboot.
# Use this BEFORE running update.sh so the changes persist.
set -eu

echo "==> Disabling read-only overlay filesystem..."
if sudo raspi-config nonint disable_overlayfs 2>/dev/null; then
    echo "    Done (used: disable_overlayfs)."
else
    echo "    Falling back to do_overlayfs 1 ..."
    sudo raspi-config nonint do_overlayfs 1
fi

echo ""
echo "==> Rebooting in 5 seconds.  After reboot, run:  bash update.sh"
sleep 5
sudo reboot
