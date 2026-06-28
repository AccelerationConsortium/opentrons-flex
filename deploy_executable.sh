#!/bin/sh
# Deploy the self-contained connector binary to the Flex.
# Does not require pip, a venv, or any Python on the robot.
#
# Usage:
#   ./deploy_executable.sh [hostname] [connector_dir]
#
# Arguments:
#   hostname       Flex hostname or IP (default: opentrons-flex)
#   connector_dir  Local directory containing the connector binary and config
#                  (default: dist_connector). Must contain:
#                    - connector  (the PyInstaller binary, built for aarch64)
#                    - flex_config.json
#
# Download the flex-connector-arm artifact from CI and unzip into
# dist_connector/ before running this script.
set -e

HOST="${1:-opentrons-flex}"
CONNECTOR_DIR="${2:-dist_connector}"
INSTALL_PATH="/var/sila2_flex"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$SCRIPT_DIR/$CONNECTOR_DIR/connector" ]; then
    echo "ERROR: connector binary not found in '$CONNECTOR_DIR/'."
    echo "Download the flex-connector-arm artifact from CI and unzip into dist_connector/."
    exit 1
fi

echo "=== Flex SiLA2 Connector Deploy (executable) ==="
echo "Host:      $HOST"
echo "Binary:    $CONNECTOR_DIR/connector"
echo "Install:   $INSTALL_PATH/connector"

echo ""
echo "Copying connector binary to $HOST ..."
ssh "root@$HOST" "mount -o remount,rw / && mkdir -p $INSTALL_PATH"
scp -O "$SCRIPT_DIR/$CONNECTOR_DIR/connector" "root@$HOST:$INSTALL_PATH/connector"
if [ -f "$SCRIPT_DIR/config/flex_config.local.json" ]; then
    CONFIG_FILE="$SCRIPT_DIR/config/flex_config.local.json"
    echo "Config: config/flex_config.local.json (local override)"
else
    CONFIG_FILE="$SCRIPT_DIR/$CONNECTOR_DIR/flex_config.json"
fi
scp -O "$CONFIG_FILE" "root@$HOST:$INSTALL_PATH/config.json"
ssh "root@$HOST" "chmod +x $INSTALL_PATH/connector"

echo ""
echo "Verifying ..."
ssh "root@$HOST" "$INSTALL_PATH/connector --help"

echo ""
echo "=== Deploy complete ==="
echo "Install the service with:"
echo "  sh scripts/install_connector_service.sh $HOST"
