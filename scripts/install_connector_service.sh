#!/bin/sh
# Install the SiLA2 connector as a systemd service on the Flex.
# Disables the Opentrons robot server first so we get exclusive hardware access.
# Usage: ./scripts/install_connector_service.sh <host>
set -e

HOST="${1:?Usage: $0 <host>}"
SCRIPT_DIR="$(dirname "$0")"

echo "Copying start_connector.sh to robot..."
scp "$SCRIPT_DIR/start_connector.sh" "root@$HOST:/data/start_connector.sh"

ssh "root@${HOST}" '
set -e
mount -o remount,rw /

# Stop/disable the stock robot server so the connector owns the CAN hardware.
# On the Flex, motion/instruments/gripper are reached over CAN through OT3API;
# there are no OT-2 Pi-GPIO units, but we still try a few known auxiliary units
# (ignored if absent) so nothing else grabs the bus or the status bar.
echo "Disabling Opentrons services that hold the hardware..."
for svc in opentrons-robot-server opentrons-status-bar opentrons-gpio-setup opentrons-status-leds; do
    systemctl disable "$svc" 2>/dev/null || true
    systemctl stop "$svc" 2>/dev/null || true
done

echo "Installing sila2-connector service..."
cat > /etc/systemd/system/sila2-connector.service << EOF
[Unit]
Description=SiLA2 Opentrons Flex Connector
After=network.target
Wants=network.target

[Service]
Type=simple
ExecStart=/var/sila2_flex/bin/connector start --app unitelabs.opentrons_flex:create_app --config-path /var/sila2_flex/config.json
# The opentrons stack auto-detects OT-3 (Flex) hardware on the device; no OT-2
# Smoothie/Pi env (RUNNING_ON_PI, OT_SMOOTHIE_ID) is set. Add any Flex-specific
# OT_* env here if a future opentrons release requires it.
Environment=PYTHONPYCACHEPREFIX=/var/cache/sila2-pycache
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable sila2-connector
systemctl restart sila2-connector
systemctl status sila2-connector --no-pager
'
