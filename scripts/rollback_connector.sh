#!/bin/sh
# Restore the previously activated connector release while leaving stock mode on.
set -eu

HOST="${1:?Usage: $0 <host>}"

ssh "root@$HOST" sh <<'REMOTE'
set -eu
STATE_DIR="/var/lib/unitelabs-opentrons-flex"
ACTIVE_PATH="/var/sila2_flex"
PREVIOUS_FILE="$STATE_DIR/previous-release"
SERVICE_STATE="$STATE_DIR/stock-service-state"
LEGACY_PATH="$STATE_DIR/legacy-pre-versioned-release"

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

if [ ! -f "$PREVIOUS_FILE" ]; then
    echo "ERROR: no previous connector release is recorded." >&2
    exit 1
fi
RECORD="$(sed -n '1p' "$PREVIOUS_FILE")"
KIND="${RECORD%%:*}"
TARGET="${RECORD#*:}"
if [ "$TARGET" = "$RECORD" ] || [ -z "$TARGET" ]; then
    echo "ERROR: invalid previous-release record." >&2
    exit 1
fi

systemctl stop sila2-connector 2>/dev/null || true
systemctl disable sila2-connector 2>/dev/null || true
restore_stock_services
if ! wait_for_stock; then
    echo "ERROR: stock robot-server did not recover within 120 seconds; release activation is unchanged." >&2
    systemctl status opentrons-robot-server --no-pager || true
    exit 1
fi

CURRENT_TARGET=""
if [ -L "$ACTIVE_PATH" ]; then
    CURRENT_TARGET="$(readlink -f "$ACTIVE_PATH")"
elif [ -d "$ACTIVE_PATH" ]; then
    if [ -e "$LEGACY_PATH" ]; then
        echo "ERROR: both the active directory and managed legacy release exist." >&2
        exit 1
    fi
    mv "$ACTIVE_PATH" "$LEGACY_PATH"
    CURRENT_TARGET="$LEGACY_PATH"
elif [ -e "$ACTIVE_PATH" ]; then
    echo "ERROR: active path is neither a directory nor symlink: $ACTIVE_PATH" >&2
    exit 1
fi

activate_target() {
    target="$1"
    next_link="$ACTIVE_PATH.rollback.$$"
    ln -s "$target" "$next_link"
    python3 -c 'import os,sys; os.replace(sys.argv[1], sys.argv[2])' "$next_link" "$ACTIVE_PATH"
    if [ "$(readlink -f "$ACTIVE_PATH")" != "$target" ]; then
        echo "ERROR: rollback activation verification failed for $target." >&2
        exit 1
    fi
}

case "$KIND" in
    release)
        case "$TARGET" in
            "$STATE_DIR"/releases/*) ;;
            *)
                echo "ERROR: recorded release is outside the managed release directory." >&2
                exit 1
                ;;
        esac
        if [ ! -x "$TARGET/bin/python" ]; then
            echo "ERROR: recorded release is unavailable: $TARGET" >&2
            exit 1
        fi
        if [ -f "$STATE_DIR/run-mutation.env" ]; then
            set -a
            . "$STATE_DIR/run-mutation.env"
            set +a
        fi
        "$TARGET/bin/python" -m unitelabs.opentrons_flex.runtime_preflight \
            --config "$TARGET/config.json" \
            --require-robot-server
        activate_target "$TARGET"
        ;;
    legacy)
        if [ "$TARGET" != "$LEGACY_PATH" ] || [ ! -d "$TARGET" ]; then
            echo "ERROR: recorded legacy release is unavailable: $TARGET" >&2
            exit 1
        fi
        activate_target "$TARGET"
        ;;
    *)
        echo "ERROR: unsupported previous-release record type: $KIND" >&2
        exit 1
        ;;
esac

case "$CURRENT_TARGET" in
    "$STATE_DIR"/releases/*)
        printf 'release:%s\n' "$CURRENT_TARGET" > "$PREVIOUS_FILE"
        ;;
    "$LEGACY_PATH")
        printf 'legacy:%s\n' "$CURRENT_TARGET" > "$PREVIOUS_FILE"
        ;;
    "")
        rm -f "$PREVIOUS_FILE"
        ;;
    *)
        echo "ERROR: previous active target is outside the managed release locations: $CURRENT_TARGET" >&2
        exit 1
        ;;
esac

echo "Rollback activation complete. Stock robot-server remains active."
echo "Run switch_mode.sh only after validating the restored connector's workflow compatibility."
rmdir "$LOCK_DIR"
trap - 0 HUP INT TERM
REMOTE
