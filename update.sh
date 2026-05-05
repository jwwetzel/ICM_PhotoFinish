#!/usr/bin/env bash
# update.sh -- check GitHub for new commits; if found, pull/install/restart.
# Run on the Pi:
#   bash update.sh           normal update (no-op if already current)
#   bash update.sh --force   reinstall and restart even if no new commits
set -eu

FORCE=0
if [ "${1:-}" = "--force" ] || [ "${1:-}" = "-f" ]; then
    FORCE=1
fi

# Always operate from the directory this script lives in.
cd "$(dirname "$0")"

echo "==> Checking GitHub for updates..."
git fetch --quiet origin

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse @{u})

if [ "$LOCAL" = "$REMOTE" ] && [ "$FORCE" -eq 0 ]; then
    echo "    Already up to date (commit ${LOCAL:0:7})."
    echo "    Nothing to do.  Use --force to reinstall anyway."
    exit 0
fi

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "    New commits available:"
    git --no-pager log --oneline "$LOCAL..$REMOTE" | sed 's/^/      /'
    echo ""
    echo "==> Pulling..."
    git pull --ff-only
else
    echo "    No new commits, but --force requested."
fi

echo ""
echo "==> Re-running installer..."
bash install.sh

echo ""
echo "==> Restarting service..."
sudo systemctl restart besttrack
sleep 1
sudo systemctl status besttrack --no-pager | head -n 12

echo ""
echo "==> Update complete."
