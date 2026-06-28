#!/bin/sh
# Deploy the SiLA2 Flex connector to the robot.
#
# Usage:
#   ./deploy.sh [hostname] [wheel_dir]
#
# Arguments:
#   hostname   Flex hostname or IP (default: opentrons-flex)
#   wheel_dir  Local directory containing built aarch64 wheels (default: dist_arm)
#
# The Flex host is aarch64 (ARM64) running a modern glibc, so unlike the OT-2
# (armv7l / glibc 2.25) the standard manylinux_2_17_aarch64 wheels from PyPI work
# directly — no from-source grpcio/OpenSSL build is required. The wheel directory
# must contain the output of the "Build Flex aarch64 Wheels" CI workflow (download
# the flex-arm-wheels artifact and unzip it into dist_arm/).

set -e

HOST="${1:-opentrons-flex}"
WHEEL_DIR="${2:-dist_arm}"
VENV_PATH="/var/sila2_flex"
REMOTE_DIR="/root/dist_arm"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$SCRIPT_DIR/config/flex_config.local.json" ]; then
    CONFIG_SRC="$SCRIPT_DIR/config/flex_config.local.json"
    echo "Config: config/flex_config.local.json (local override)"
else
    CONFIG_SRC="$SCRIPT_DIR/config/flex_config.json"
fi

if [ ! -d "$SCRIPT_DIR/$WHEEL_DIR" ]; then
    echo "ERROR: Wheel directory '$WHEEL_DIR' not found."
    echo "Download the flex-arm-wheels artifact from CI and unzip into dist_arm/."
    exit 1
fi

echo "=== Flex SiLA2 Connector Deploy ==="
echo "Host:      $HOST"
echo "Wheels:    $WHEEL_DIR"
echo "Venv:      $VENV_PATH"

echo ""
echo "Copying wheels and scripts to $HOST:$REMOTE_DIR ..."
ssh "root@$HOST" "rm -rf $REMOTE_DIR && mkdir -p $REMOTE_DIR"
scp -O "$SCRIPT_DIR/scripts/install.sh" "$SCRIPT_DIR/$WHEEL_DIR"/*.whl "root@$HOST:$REMOTE_DIR/"
scp -O "$CONFIG_SRC" "root@$HOST:$REMOTE_DIR/flex_config.json"

echo ""
echo "Installing on robot ..."
ssh "root@$HOST" "rm -rf $VENV_PATH /var/user-packages/var/sila2_flex && sh $REMOTE_DIR/install.sh $VENV_PATH"

echo ""
echo "Verifying ..."
ssh "root@$HOST" "$VENV_PATH/bin/python -c 'import grpc, unitelabs.opentrons_flex; print(\"OK grpc=\"+grpc.__version__)'"

echo ""
echo "=== Deploy complete ==="
echo "Install and start the service with:"
echo "  sh scripts/install_connector_service.sh $HOST"
