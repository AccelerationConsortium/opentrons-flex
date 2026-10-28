#!/bin/sh
# Install and start connector mode only after no-hardware runtime validation.
set -eu

HOST="${1:?Usage: $0 <host>}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"

scp "$SCRIPT_DIR/start_connector.sh" "root@$HOST:/data/start_connector.sh"

ssh "root@$HOST" sh <<'REMOTE'
set -eu

STATE_DIR="/var/lib/unitelabs-opentrons-flex"
ACTIVE_PATH="/var/sila2_flex"
ENV_FILE="$STATE_DIR/run-mutation.env"
CONFIG_SELECTOR="$STATE_DIR/connector-config.json"
SERVICE_STATE="$STATE_DIR/stock-service-state"
HARDWARE_SERVICES="opentrons-robot-server opentrons-status-bar opentrons-gpio-setup opentrons-status-leds"

if [ ! -L "$ACTIVE_PATH" ] || [ ! -x "$ACTIVE_PATH/bin/python" ]; then
    echo "ERROR: no activated versioned connector at $ACTIVE_PATH; run deploy.sh first." >&2
    exit 1
fi
if ! grep -q "^UNITELABS_RUN_MUTATION_TOKEN=.\{32\}.*" "$ENV_FILE" 2>/dev/null; then
    echo "ERROR: $ENV_FILE must contain UNITELABS_RUN_MUTATION_TOKEN with at least 32 characters." >&2
    exit 1
fi
if ! grep -q "^UNITELABS_RUN_MUTATION_ACTOR=..*" "$ENV_FILE" 2>/dev/null; then
    echo "ERROR: $ENV_FILE must contain UNITELABS_RUN_MUTATION_ACTOR." >&2
    exit 1
fi
chmod 600 "$ENV_FILE"
set -a
. "$ENV_FILE"
set +a

# Import every private API needed by connector mode before touching a service
# or initializing the CAN hardware.
"$ACTIVE_PATH/bin/python" -m unitelabs.opentrons_flex.runtime_preflight \
    --config "$ACTIVE_PATH/config.json" \
    --require-robot-server \
    --require-mutation \
    --require-live-hardware

mount -o remount,rw /
mkdir -p "$STATE_DIR"
LOCK_DIR="$STATE_DIR/operation.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "ERROR: another Flex deployment or mode transition is in progress ($LOCK_DIR)." >&2
    exit 1
fi
release_operation_lock() {
    rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap release_operation_lock 0 HUP INT TERM

if systemctl is-active --quiet sila2-connector 2>/dev/null; then
    echo "ERROR: connector is already active; switch to stock mode before reinstalling its service." >&2
    exit 1
fi
ln -sfn "$ACTIVE_PATH/config.json" "$CONFIG_SELECTOR"

# Run the connector with the same environment as the stock robot-server, so the
# Opentrons libraries detect the robot (RUNNING_ON_VERDIN: calibration from
# /data) and the embedded robot-server keeps its data where the stock one does.
# Interpreter variables (PYTHONPATH etc.) are skipped: the stock ones point at
# /opt/opentrons-robot-server and would shadow the release's own packages.
ROBOT_ENV_FILE="$STATE_DIR/robot-environment.env"
systemctl show opentrons-robot-server -p Environment --value 2>/dev/null | python3 -c '
import shlex, sys
for item in shlex.split(sys.stdin.read()):
    key, sep, value = item.partition("=")
    if sep and key.replace("_", "").isalnum() and not key.startswith("PYTHON"):
        print(key + "=" + shlex.quote(value))
' > "$ROBOT_ENV_FILE.tmp"
if ! grep -qE "^RUNNING_ON_(VERDIN|PI)=" "$ROBOT_ENV_FILE.tmp"; then
    echo "The stock robot-server unit sets no RUNNING_ON_VERDIN; adding it (this is a Flex)."
    echo "RUNNING_ON_VERDIN=true" >> "$ROBOT_ENV_FILE.tmp"
fi
mv "$ROBOT_ENV_FILE.tmp" "$ROBOT_ENV_FILE"
echo "Connector environment copied from the stock robot-server:"
sed 's/^/  /' "$ROBOT_ENV_FILE"
STOCK_ENV_FILES="$(systemctl show opentrons-robot-server -p EnvironmentFiles --value 2>/dev/null |
    sed -n 's/^\([^ ]*\) (ignore_errors=.*/\1/p')"
EXTRA_ENV_LINES=""
for env_file in $STOCK_ENV_FILES; do
    EXTRA_ENV_LINES="${EXTRA_ENV_LINES}EnvironmentFile=-${env_file}
"
done

cat > /etc/systemd/system/sila2-connector.service <<EOF
[Unit]
Description=SiLA2 Opentrons Flex Connector
After=network.target
Wants=network.target
# Never stop retrying: an unattended robot must come back on its own.
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=$ACTIVE_PATH/bin/connector start --app unitelabs.opentrons_flex:create_app --config-path $CONFIG_SELECTOR
${EXTRA_ENV_LINES}EnvironmentFile=-$ROBOT_ENV_FILE
Environment=PYTHONPYCACHEPREFIX=/var/cache/sila2-pycache
EnvironmentFile=-$ENV_FILE
# Type=notify, the startup timeout, and the watchdog are set by a drop-in that
# configure_connector_watchdog writes only for releases that support them.
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
configure_connector_watchdog() {
    # Releases that report readiness and send heartbeats run as Type=notify with a
    # systemd watchdog; older releases (for example after a rollback) run without it.
    dropin_dir="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}/sila2-connector.service.d"
    mkdir -p "$dropin_dir"
    set -- "$ACTIVE_PATH"/lib/python3*/site-packages/unitelabs/opentrons_flex/runtime_environment.py
    if [ -f "$1" ]; then
        printf '%s\n' '[Service]' 'Type=notify' 'NotifyAccess=main' 'TimeoutStartSec=300' 'WatchdogSec=120' \
            > "$dropin_dir/watchdog.conf"
        echo "systemd watchdog enabled for the active release."
    else
        rm -f "$dropin_dir/watchdog.conf"
        echo "Active release has no systemd watchdog support; running without it."
    fi
    systemctl daemon-reload
}
configure_connector_watchdog

# Preserve the prior service state for automatic failure recovery and explicit
# rollback. Unknown services are ignored.
: > "$SERVICE_STATE"
for svc in $HARDWARE_SERVICES; do
    if systemctl cat "$svc" >/dev/null 2>&1; then
        ENABLED=no
        ACTIVE=no
        systemctl is-enabled --quiet "$svc" 2>/dev/null && ENABLED=yes
        systemctl is-active --quiet "$svc" 2>/dev/null && ACTIVE=yes
        printf '%s|%s|%s\n' "$svc" "$ENABLED" "$ACTIVE" >> "$SERVICE_STATE"
    fi
done

restore_stock() {
    echo "Restoring stock Opentrons services..."
    systemctl stop sila2-connector 2>/dev/null || true
    systemctl disable sila2-connector 2>/dev/null || true
    while IFS='|' read -r svc enabled active; do
        [ -n "$svc" ] || continue
        if [ "$enabled" = yes ]; then
            systemctl enable "$svc" 2>/dev/null || true
        fi
        if [ "$active" = yes ] || [ "$svc" = opentrons-robot-server ]; then
            systemctl start "$svc" 2>/dev/null || true
        fi
    done < "$SERVICE_STATE"
    i=60
    while [ "$i" -gt 0 ]; do
        if systemctl is-active --quiet opentrons-robot-server 2>/dev/null &&
            curl --fail --silent -H 'Opentrons-Version: *' --connect-timeout 2 --max-time 5 \
                http://127.0.0.1:31950/health >/dev/null 2>&1; then
            echo "Stock Opentrons mode restored and healthy."
            return 0
        fi
        i=$((i - 1))
        sleep 2
    done
    echo "ERROR: stock robot-server did not recover within 120 seconds." >&2
    systemctl status opentrons-robot-server --no-pager || true
    return 1
}

stock_services_stopped_and_disabled() {
    for svc in $HARDWARE_SERVICES; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            return 1
        fi
        if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
            return 1
        fi
    done
    return 0
}

echo "Stopping stock hardware owners..."
for svc in $HARDWARE_SERVICES; do
    systemctl stop "$svc" 2>/dev/null || true
    systemctl disable "$svc" 2>/dev/null || true
done
if ! stock_services_stopped_and_disabled; then
    echo "ERROR: a stock hardware-owner service remained active or enabled; refusing dual OT3API ownership." >&2
    restore_stock
    exit 1
fi

systemctl reset-failed sila2-connector 2>/dev/null || true
if ! systemctl start sila2-connector; then
    systemctl status sila2-connector --no-pager || true
    restore_stock
    exit 1
fi

echo "Waiting up to 120 seconds for gRPC and embedded HTTP..."
READY=no
i=60
while [ "$i" -gt 0 ]; do
    if python3 -c "import socket; s=socket.create_connection(('127.0.0.1',50051),2); s.close()" 2>/dev/null &&
        curl --fail --silent -H 'Opentrons-Version: *' --show-error --connect-timeout 2 --max-time 5 \
            http://127.0.0.1:31950/health >/dev/null 2>&1 &&
        stock_services_stopped_and_disabled; then
        READY=yes
        break
    fi
    i=$((i - 1))
    sleep 2
done

if [ "$READY" != yes ]; then
    echo "ERROR: connector failed the bounded startup health check." >&2
    systemctl status sila2-connector --no-pager || true
    journalctl -u sila2-connector -n 100 --no-pager || true
    restore_stock
    exit 1
fi

# These two read-only requests catch the exact failure mode where /health works
# but robot-server state or connector-owned mutation routes were not initialized.
if ! curl --fail --silent -H 'Opentrons-Version: *' --show-error --connect-timeout 2 --max-time 10 \
    http://127.0.0.1:31950/deck_configuration >/dev/null; then
    echo "ERROR: embedded /deck_configuration did not answer within 10 seconds." >&2
    restore_stock
    exit 1
fi
if ! curl --fail --silent -H 'Opentrons-Version: *' --show-error --connect-timeout 2 --max-time 10 \
    http://127.0.0.1:31950/openapi.json |
    python3 -c "import json,sys; paths=json.load(sys.stdin).get('paths',{}); assert '/unitelabs/runs/{run_id}/mutations' in paths"; then
    echo "ERROR: controlled mutation routes are absent from embedded robot-server." >&2
    restore_stock
    exit 1
fi

systemctl enable sila2-connector
if ! stock_services_stopped_and_disabled; then
    echo "ERROR: stock hardware ownership reappeared after connector startup." >&2
    restore_stock
    exit 1
fi

echo "Connector mode is healthy."
systemctl status sila2-connector --no-pager
rmdir "$LOCK_DIR"
trap - 0 HUP INT TERM
REMOTE
