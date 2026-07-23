#!/bin/sh
# Safely switch exclusive Flex hardware ownership between connector and stock.
set -eu

HOST="${1:?Usage: $0 <host> <connector|opentrons>}"
MODE="${2:?Usage: $0 <host> <connector|opentrons>}"
case "$MODE" in
    connector|opentrons) ;;
    *)
        echo "ERROR: mode must be connector or opentrons" >&2
        exit 1
        ;;
esac

ssh "root@$HOST" sh -s -- "$MODE" <<'REMOTE'
set -eu
MODE="$1"
STATE_DIR="/var/lib/unitelabs-opentrons-flex"
ACTIVE_PATH="/var/sila2_flex"
ENV_FILE="$STATE_DIR/run-mutation.env"
SERVICE_STATE="$STATE_DIR/stock-service-state"
HARDWARE_SERVICES="opentrons-robot-server opentrons-status-bar opentrons-gpio-setup opentrons-status-leds"

restore_stock_services() {
    if [ -f "$SERVICE_STATE" ]; then
        while IFS='|' read -r svc enabled active; do
            [ -n "$svc" ] || continue
            if [ "$enabled" = yes ]; then
                systemctl enable "$svc" 2>/dev/null || true
            fi
            if [ "$active" = yes ] || [ "$svc" = opentrons-robot-server ]; then
                systemctl start "$svc" 2>/dev/null || true
            fi
        done < "$SERVICE_STATE"
    else
        systemctl enable opentrons-robot-server 2>/dev/null || true
        systemctl start opentrons-robot-server
    fi
}

stop_stock_services() {
    for svc in $HARDWARE_SERVICES; do
        systemctl stop "$svc" 2>/dev/null || true
    done
}

wait_for_connector() {
    i=60
    while [ "$i" -gt 0 ]; do
        if python3 -c "import socket; s=socket.create_connection(('127.0.0.1',50051),2); s.close()" 2>/dev/null &&
            curl --fail --silent --connect-timeout 2 --max-time 5 \
                http://127.0.0.1:31950/health >/dev/null 2>&1; then
            curl --fail --silent --connect-timeout 2 --max-time 10 \
                http://127.0.0.1:31950/deck_configuration >/dev/null &&
            curl --fail --silent --connect-timeout 2 --max-time 10 \
                http://127.0.0.1:31950/openapi.json |
                python3 -c "import json,sys; assert '/unitelabs/runs/{run_id}/mutations' in json.load(sys.stdin).get('paths',{})"
            return $?
        fi
        i=$((i - 1))
        sleep 2
    done
    return 1
}

wait_for_stock() {
    i=60
    while [ "$i" -gt 0 ]; do
        if systemctl is-active --quiet opentrons-robot-server 2>/dev/null &&
            curl --fail --silent --connect-timeout 2 --max-time 5 \
                http://127.0.0.1:31950/health >/dev/null 2>&1; then
            return 0
        fi
        i=$((i - 1))
        sleep 2
    done
    return 1
}

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
    CURRENT=connector
elif systemctl is-active --quiet opentrons-robot-server 2>/dev/null; then
    CURRENT=opentrons
else
    CURRENT=none
fi
echo "Current: $CURRENT -> Target: $MODE"

if [ "$MODE" = connector ]; then
    if [ ! -L "$ACTIVE_PATH" ] || [ ! -x "$ACTIVE_PATH/bin/python" ]; then
        echo "ERROR: no activated versioned connector; run deploy.sh first." >&2
        exit 1
    fi
    if [ ! -f "$ENV_FILE" ]; then
        echo "ERROR: missing $ENV_FILE." >&2
        exit 1
    fi
    set -a
    . "$ENV_FILE"
    set +a
    "$ACTIVE_PATH/bin/python" -m unitelabs.opentrons_flex.runtime_preflight \
        --config "$ACTIVE_PATH/config.json" \
        --require-robot-server \
        --require-mutation \
        --require-live-hardware

    if [ "$CURRENT" = connector ] && wait_for_connector; then
        echo "Connector mode is already healthy."
        exit 0
    fi

    systemctl stop sila2-connector 2>/dev/null || true
    stop_stock_services
    systemctl reset-failed sila2-connector 2>/dev/null || true
    if ! systemctl start sila2-connector || ! wait_for_connector; then
        echo "ERROR: connector mode failed; restoring stock Opentrons mode." >&2
        systemctl status sila2-connector --no-pager || true
        journalctl -u sila2-connector -n 100 --no-pager || true
        systemctl stop sila2-connector 2>/dev/null || true
        systemctl disable sila2-connector 2>/dev/null || true
        restore_stock_services
        if ! wait_for_stock; then
            echo "ERROR: stock robot-server did not recover within 120 seconds." >&2
            systemctl status opentrons-robot-server --no-pager || true
        fi
        exit 1
    fi
    for svc in $HARDWARE_SERVICES; do
        systemctl disable "$svc" 2>/dev/null || true
    done
    systemctl enable sila2-connector
    echo "Connector mode healthy: gRPC 50051, embedded HTTP 31950."
    exit 0
fi

if [ "$CURRENT" = opentrons ] && wait_for_stock; then
    echo "Stock Opentrons mode is already healthy."
    exit 0
fi

systemctl stop sila2-connector 2>/dev/null || true
systemctl disable sila2-connector 2>/dev/null || true
restore_stock_services
if ! wait_for_stock; then
    echo "ERROR: stock robot-server failed its bounded health check." >&2
    systemctl status opentrons-robot-server --no-pager || true
    journalctl -u opentrons-robot-server -n 100 --no-pager || true
    exit 1
fi
echo "Stock Opentrons mode healthy: HTTP 31950; SiLA gRPC disabled."
REMOTE
