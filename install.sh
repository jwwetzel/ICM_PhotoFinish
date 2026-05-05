#!/usr/bin/env bash
# install.sh -- deploy bestTrack photo finish on Raspberry Pi
# Run as: bash install.sh
set -eu

SCRIPT="bestTrack.py"
SERVICE="besttrack.service"

# --- Auto-detect target user and home ---------------------------------------
# When run with sudo, $USER is "root" but $SUDO_USER is the real account.
TARGET_USER="${SUDO_USER:-$USER}"
TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)

if [ -z "$TARGET_HOME" ] || [ ! -d "$TARGET_HOME" ]; then
    echo "ERROR: could not determine home directory for user '$TARGET_USER'"
    exit 1
fi

SERVICE_DIR="/etc/systemd/system"
AUTOSTART_DIR="$TARGET_HOME/.config/autostart"
DEST_SCRIPT="$TARGET_HOME/$SCRIPT"
SRC_SCRIPT="$(pwd)/$SCRIPT"

echo "=== BestTrack Photo Finish installer ==="
echo "    Target user:    $TARGET_USER"
echo "    Target home:    $TARGET_HOME"
echo "    Script source:  $SRC_SCRIPT"
echo "    Script install: $DEST_SCRIPT"
echo ""

# --- 0. Strip Windows line endings (CRLF -> LF) -----------------------------
# Files moved via USB stick from a Mac/Windows host often pick up \r\n,
# which breaks shell scripts and confuses systemd. Self-heal.
echo "[0/5] Normalizing line endings..."
for f in "$SCRIPT" "$SERVICE" "$0"; do
    if [ -f "$f" ] && grep -q $'\r' "$f" 2>/dev/null; then
        echo "    fixing CRLF in $f"
        sed -i 's/\r$//' "$f"
    fi
done

# --- 1. Dependencies --------------------------------------------------------
# apt (not pip) on modern Raspbian (Bookworm+) -- system Python is PEP 668
# "externally managed" and refuses global pip installs. python3-tk is needed
# for the fullscreen GUI.
echo "[1/5] Installing Python dependencies via apt..."
sudo apt-get update -qq
sudo apt-get install -y python3-serial python3-rpi.gpio python3-tk

# --- 2. Script --------------------------------------------------------------
echo "[2/5] Installing $SCRIPT to $DEST_SCRIPT"
if [ -f "$DEST_SCRIPT" ] && [ "$SRC_SCRIPT" -ef "$DEST_SCRIPT" ]; then
    echo "    source and destination are the same file -- skipping copy"
else
    cp "$SCRIPT" "$DEST_SCRIPT"
fi
chmod +x "$DEST_SCRIPT"

# --- 3. Systemd service -----------------------------------------------------
# Patch the service file in-flight so it matches the real user/home,
# regardless of what's hardcoded in the source.
echo "[3/5] Installing systemd service for user '$TARGET_USER'..."
TMP_SERVICE=$(mktemp)
sed \
    -e "s|^User=.*|User=$TARGET_USER|" \
    -e "s|^Group=.*|Group=$TARGET_USER|" \
    -e "s|/home/[^/[:space:]]*/bestTrack\.py|$DEST_SCRIPT|g" \
    -e "s|XAUTHORITY=/home/[^/[:space:]]*/\.Xauthority|XAUTHORITY=$TARGET_HOME/.Xauthority|g" \
    -e "s|file:///home/[^/[:space:]]*/bestTrack\.py|file://$DEST_SCRIPT|g" \
    "$SERVICE" > "$TMP_SERVICE"

sudo cp "$TMP_SERVICE" "$SERVICE_DIR/$SERVICE"
sudo chmod 644 "$SERVICE_DIR/$SERVICE"
rm -f "$TMP_SERVICE"

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"

# --- 4. Remove stale XDG autostart entries ----------------------------------
# Earlier installer versions added a .desktop autostart entry as a backup
# launcher.  That caused TWO copies of the kiosk to boot (one from systemd,
# one from the desktop session).  systemd is the source of truth -- nuke
# any stray .desktop file from previous installs.
echo "[4/5] Removing any stale XDG autostart entries..."
rm -f "$AUTOSTART_DIR/besttrack.desktop"
rm -f "$TARGET_HOME/.config/autostart/besttrack.desktop"

# --- 5. Summary -------------------------------------------------------------
echo "[5/5] Done."
echo ""
echo "=== Installation complete ==="
echo ""
echo "Service status:"
sudo systemctl status "$SERVICE" --no-pager || true
echo ""
echo "Useful commands:"
echo "  sudo systemctl start besttrack     - start now"
echo "  sudo systemctl stop besttrack      - stop"
echo "  sudo systemctl restart besttrack   - restart"
echo "  journalctl -u besttrack -f         - live log"
echo ""
echo "Reboot the Pi to confirm auto-start works:"
echo "  sudo reboot"
