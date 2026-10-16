#!/bin/sh
# Preserve and reset the ASMS unit deck ledger after a physical reconciliation.
set -eu

HOST="${1:?Usage: $0 <host> <operator> <confirmation>}"
OPERATOR="${2:?Usage: $0 <host> <operator> <confirmation>}"
CONFIRMATION="${3:?Usage: $0 <host> <operator> <confirmation>}"
EXPECTED="ASMS-UNIT-RECONCILE-A1-B1-B2-EMPTY"

case "$OPERATOR" in
    *[!A-Za-z0-9._-]*|"")
        echo "ERROR: operator must contain only letters, numbers, dot, underscore, or hyphen." >&2
        exit 1
        ;;
esac
if [ "$CONFIRMATION" != "$EXPECTED" ]; then
    echo "ERROR: confirmation must exactly equal $EXPECTED" >&2
    exit 1
fi

ssh "root@$HOST" sh -s -- "$OPERATOR" "$CONFIRMATION" <<'REMOTE'
set -eu
OPERATOR="$1"
CONFIRMATION="$2"
EXPECTED="ASMS-UNIT-RECONCILE-A1-B1-B2-EMPTY"
STATE_DIR="/var/lib/unitelabs-opentrons-flex"
LEDGER="$STATE_DIR/asms-unit-deck-state.json"
AUDIT="$STATE_DIR/asms-unit-deck-reconciliations.jsonl"

if [ "$CONFIRMATION" != "$EXPECTED" ]; then
    echo "ERROR: remote confirmation mismatch." >&2
    exit 1
fi
mount -o remount,rw /
mkdir -p "$STATE_DIR"
LOCK_DIR="$STATE_DIR/operation.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "ERROR: another Flex deployment or mode transition is in progress ($LOCK_DIR)." >&2
    exit 1
fi
release_operation_lock() {
    if [ -n "${TEMP_LEDGER:-}" ]; then
        rm -f "$TEMP_LEDGER"
    fi
    rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap release_operation_lock 0 HUP INT TERM

if systemctl is-active --quiet sila2-connector 2>/dev/null; then
    echo "ERROR: switch to stock Opentrons mode before reconciling the connector ledger." >&2
    exit 1
fi
if [ ! -f "$LEDGER" ]; then
    echo "ERROR: ASMS unit deck ledger is missing; refusing to bootstrap an uncertain prior state." >&2
    exit 1
fi

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
BACKUP="$LEDGER.reconciled-$TIMESTAMP"
if [ -e "$BACKUP" ]; then
    echo "ERROR: reconciliation backup already exists: $BACKUP" >&2
    exit 1
fi
cp -p "$LEDGER" "$BACKUP"
cmp "$LEDGER" "$BACKUP"
sync

if ! python3 -c \
    "import json,sys; print(json.dumps(json.load(open(sys.argv[1], encoding='utf-8')), indent=2, sort_keys=True))" \
    "$BACKUP"; then
    echo "WARNING: preserved ledger is malformed; continuing only because the byte-for-byte backup succeeded." >&2
fi

TEMP_LEDGER="$STATE_DIR/.asms-unit-deck-state.reconcile-$$.json"
umask 077
printf '%s\n' \
    '{"version":1,"valid":true,"clean_shutdown":true,"occupancy":{"A1":"asms-plate","B1":"elution-plate"}}' \
    > "$TEMP_LEDGER"
sync

printf '{"timestamp":"%s","operator":"%s","confirmation":"%s","preservedLedger":"%s","action":"authorized-reset-to-A1-B1"}\n' \
    "$TIMESTAMP" "$OPERATOR" "$CONFIRMATION" "$BACKUP" >> "$AUDIT"
sync

if systemctl is-active --quiet sila2-connector 2>/dev/null; then
    echo "ERROR: connector became active during reconciliation; preserved backup and audit, but did not reset ledger." >&2
    exit 1
fi
mv "$TEMP_LEDGER" "$LEDGER"
TEMP_LEDGER=""
sync

echo "Preserved the prior ledger at $BACKUP"
echo "Recorded the authorized reset in $AUDIT"
echo "Reset the ledger to A1=asms-plate, B1=elution-plate, and B2 empty."
REMOTE
