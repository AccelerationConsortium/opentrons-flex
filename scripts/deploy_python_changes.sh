#!/bin/sh
# Usage: ./scripts/deploy_python_changes.sh <host> [venv_path]
# Syncs src/unitelabs/opentrons_flex/ straight into the installed package in
# the venv on the robot — no wheel rebuild, no reinstall.
set -e

HOST="${1:?Usage: $0 <host> [venv_path]}"
VENV="${2:-/var/sila2_flex}"
LOCAL_SRC="$(dirname "$0")/../src/unitelabs/opentrons_flex/"

# Resolve the site-packages path on the robot regardless of the python minor
# version the Flex ships (e.g. python3.10 vs python3.12).
REMOTE_PKG="$(ssh "root@$HOST" "ls -d $VENV/lib/python*/site-packages/unitelabs/opentrons_flex 2>/dev/null | head -1")"
if [ -z "$REMOTE_PKG" ]; then
    echo "ERROR: could not find installed unitelabs/opentrons_flex under $VENV on $HOST"
    exit 1
fi

echo "Copying to root@$HOST:$REMOTE_PKG ..."
scp -r "$LOCAL_SRC"/* "root@$HOST:$REMOTE_PKG/"

echo "Clearing __pycache__ on robot..."
ssh "root@$HOST" "find '$REMOTE_PKG' -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true"

echo "Restarting sila2-connector service on robot..."
ssh "root@$HOST" "systemctl restart sila2-connector"
sleep 2
ssh "root@$HOST" "systemctl is-active sila2-connector"

echo "Done. Logs: ssh root@$HOST 'journalctl -u sila2-connector -f'"
