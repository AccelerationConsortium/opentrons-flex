#!/bin/sh
# Safely switch exclusive Flex hardware ownership between connector and stock.
set -eu

HOST="${1:?Usage: $0 <host> <connector-pe|connector-unit|opentrons>}"
MODE="${2:?Usage: $0 <host> <connector-pe|connector-unit|opentrons>}"
case "$MODE" in
    connector) MODE=connector-pe ;;
    connector-pe|connector-unit|opentrons) ;;
    *)
        echo "ERROR: mode must be connector-pe, connector-unit, or opentrons" >&2
        exit 1
        ;;
esac

ssh "root@$HOST" sh -s -- "$MODE" <<'REMOTE'
set -eu
MODE="$1"
STATE_DIR="/var/lib/unitelabs-opentrons-flex"
ACTIVE_PATH="/var/sila2_flex"
ENV_FILE="$STATE_DIR/run-mutation.env"
CONFIG_SELECTOR="$STATE_DIR/connector-config.json"
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

stop_and_disable_stock_services() {
    for svc in $HARDWARE_SERVICES; do
        systemctl stop "$svc" 2>/dev/null || true
        systemctl disable "$svc" 2>/dev/null || true
    done
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

unit_http_is_unavailable() {
    ! curl --fail --silent --connect-timeout 2 --max-time 5 \
        http://127.0.0.1:31950/health >/dev/null 2>&1
}

wait_for_pe_connector() {
    i=60
    while [ "$i" -gt 0 ]; do
        if python3 -c "import socket; s=socket.create_connection(('127.0.0.1',50051),2); s.close()" 2>/dev/null &&
            curl --fail --silent --connect-timeout 2 --max-time 5 \
                http://127.0.0.1:31950/health >/dev/null 2>&1; then
            curl --fail --silent --connect-timeout 2 --max-time 10 \
                http://127.0.0.1:31950/deck_configuration >/dev/null &&
            curl --fail --silent --connect-timeout 2 --max-time 10 \
                http://127.0.0.1:31950/openapi.json |
                python3 -c "import json,sys; assert '/unitelabs/runs/{run_id}/mutations' in json.load(sys.stdin).get('paths',{})" &&
            stock_services_stopped_and_disabled
            return $?
        fi
        i=$((i - 1))
        sleep 2
    done
    return 1
}

wait_for_unit_connector() {
    i=60
    while [ "$i" -gt 0 ]; do
        if python3 -c "import socket; s=socket.create_connection(('127.0.0.1',50051),2); s.close()" 2>/dev/null &&
            "$ACTIVE_PATH/bin/python" -m unitelabs.opentrons_flex.asms_unit_operations \
                health \
                --host 127.0.0.1 \
                --port 50051 \
                --manifest "$ACTIVE_PATH/asms-unit-operations.json" >/dev/null 2>&1 &&
            unit_http_is_unavailable &&
            stock_services_stopped_and_disabled; then
            return 0
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
    case "$(readlink "$CONFIG_SELECTOR" 2>/dev/null || true)" in
        */unit-config.json) CURRENT=connector-unit ;;
        *) CURRENT=connector-pe ;;
    esac
elif systemctl is-active --quiet opentrons-robot-server 2>/dev/null; then
    CURRENT=opentrons
else
    CURRENT=none
fi
echo "Current: $CURRENT -> Target: $MODE"

if [ "$MODE" = connector-pe ] || [ "$MODE" = connector-unit ]; then
    if [ ! -L "$ACTIVE_PATH" ] || [ ! -x "$ACTIVE_PATH/bin/python" ]; then
        echo "ERROR: no activated versioned connector; run deploy.sh first." >&2
        exit 1
    fi
    if ! systemctl cat sila2-connector 2>/dev/null | grep -q "$CONFIG_SELECTOR"; then
        echo "ERROR: installed sila2-connector service predates selectable configs; rerun install_connector_service.sh." >&2
        exit 1
    fi

    if [ "$MODE" = connector-pe ]; then
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
        TARGET_CONFIG="$ACTIVE_PATH/config.json"
        if [ "$CURRENT" = connector-pe ] && wait_for_pe_connector; then
            echo "Connector PE mode is already healthy."
            exit 0
        fi
    else
        "$ACTIVE_PATH/bin/python" -m unitelabs.opentrons_flex.runtime_preflight \
            --config "$ACTIVE_PATH/unit-config.json" \
            --require-sila-only \
            --require-live-hardware
        "$ACTIVE_PATH/bin/python" -c \
            "from unitelabs.opentrons_flex.asms_unit_operations import load_manifest; load_manifest('$ACTIVE_PATH/asms-unit-operations.json')"
        TARGET_CONFIG="$ACTIVE_PATH/unit-config.json"
        if [ "$CURRENT" = connector-unit ] && wait_for_unit_connector; then
            echo "Connector unit-operation mode is already healthy."
            exit 0
        fi
    fi

    systemctl stop sila2-connector 2>/dev/null || true
    stop_and_disable_stock_services
    if ! stock_services_stopped_and_disabled; then
        echo "ERROR: a stock hardware-owner service remained active or enabled; refusing dual OT3API ownership." >&2
        restore_stock_services
        exit 1
    fi
    ln -sfn "$TARGET_CONFIG" "$CONFIG_SELECTOR"
    systemctl reset-failed sila2-connector 2>/dev/null || true
    STARTED=no
    if systemctl start sila2-connector; then
        if [ "$MODE" = connector-pe ] && wait_for_pe_connector; then
            STARTED=yes
        elif [ "$MODE" = connector-unit ] && wait_for_unit_connector; then
            STARTED=yes
        fi
    fi
    if [ "$STARTED" != yes ]; then
        echo "ERROR: $MODE failed; restoring stock Opentrons mode." >&2
        if [ "$MODE" = connector-unit ]; then
            "$ACTIVE_PATH/bin/python" -m unitelabs.opentrons_flex.asms_unit_operations \
                health \
                --host 127.0.0.1 \
                --port 50051 \
                --manifest "$ACTIVE_PATH/asms-unit-operations.json" || true
        fi
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
    systemctl enable sila2-connector
    if ! stock_services_stopped_and_disabled; then
        echo "ERROR: stock hardware ownership reappeared after connector startup; restoring stock mode." >&2
        systemctl stop sila2-connector 2>/dev/null || true
        systemctl disable sila2-connector 2>/dev/null || true
        restore_stock_services
        exit 1
    fi
    if [ "$MODE" = connector-pe ]; then
        echo "Connector PE mode healthy: gRPC 50051, embedded HTTP 31950."
    else
        echo "Connector unit-operation mode healthy: SiLA gRPC 50051 exclusively owns OT3API; HTTP is disabled."
    fi
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
