#!/bin/sh
# Verify, install, preflight, and atomically activate a Flex connector release.
set -eu

HOST="${1:-opentrons-flex}"
WHEEL_DIR="${2:-dist_arm}"
STATE_DIR="/var/lib/unitelabs-opentrons-flex"
ACTIVE_PATH="/var/sila2_flex"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
case "$WHEEL_DIR" in
    /*) ARTIFACT_DIR="$WHEEL_DIR" ;;
    *) ARTIFACT_DIR="$SCRIPT_DIR/$WHEEL_DIR" ;;
esac
MANIFEST_TOOL="$SCRIPT_DIR/scripts/artifact_manifest.py"

# Runtime contract. Keep in sync with runtime_compat.py (enforced by
# tests/test_runtime_contract_consistency.py); the connector version is read
# from pyproject.toml so it can never drift from the built wheel.
OPENTRONS_VERSION=9.0.0
OPENTRONS_SOURCE_COMMIT=44b37a2f91520bf2e7245c70bf799d46c8c2d9a5
PYTHON_VERSION=3.12
ARCHITECTURE=aarch64
CONNECTOR_VERSION="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$SCRIPT_DIR/pyproject.toml" | head -n 1)"
if [ -z "$CONNECTOR_VERSION" ]; then
    echo "ERROR: cannot read the connector version from pyproject.toml." >&2
    exit 1
fi

if [ -f "$SCRIPT_DIR/config/flex_config.local.json" ]; then
    CONFIG_SRC="$SCRIPT_DIR/config/flex_config.local.json"
    echo "Config: config/flex_config.local.json (local override)"
else
    CONFIG_SRC="$SCRIPT_DIR/config/flex_config.json"
fi
UNIT_CONFIG_SRC="$SCRIPT_DIR/config/flex_unit_operations.json"
UNIT_MOVEMENT_SRC="$SCRIPT_DIR/config/asms_unit_labware_movement.json"
UNIT_OPERATIONS_SRC="$SCRIPT_DIR/config/asms_unit_operations.json"

if [ ! -d "$ARTIFACT_DIR" ]; then
    echo "ERROR: wheel directory '$ARTIFACT_DIR' not found." >&2
    echo "Download and extract the flex-arm-wheels artifact first." >&2
    exit 1
fi
if [ ! -f "$ARTIFACT_DIR/runtime-manifest.json" ]; then
    echo "ERROR: '$ARTIFACT_DIR' has no runtime-manifest.json; it predates verified releases." >&2
    echo "Rebuild it (see README 'Building dist_arm/') or download a fresh flex-arm-wheels artifact." >&2
    exit 1
fi
BUILT_VERSION="$(python3 "$MANIFEST_TOOL" field "$ARTIFACT_DIR" connectorVersion 2>/dev/null || true)"
if [ "$BUILT_VERSION" != "$CONNECTOR_VERSION" ]; then
    echo "ERROR: '$ARTIFACT_DIR' was built for connector ${BUILT_VERSION:-unknown}, but this checkout is $CONNECTOR_VERSION." >&2
    echo "Rebuild the wheels from this checkout before deploying." >&2
    exit 1
fi

python3 "$MANIFEST_TOOL" verify "$ARTIFACT_DIR" \
    --connector-version "$CONNECTOR_VERSION" \
    --opentrons-version "$OPENTRONS_VERSION" \
    --robot-server-version "$OPENTRONS_VERSION" \
    --opentrons-source-commit "$OPENTRONS_SOURCE_COMMIT" \
    --python-version "$PYTHON_VERSION" \
    --architecture "$ARCHITECTURE"
RELEASE_ID="$(python3 "$MANIFEST_TOOL" field "$ARTIFACT_DIR" releaseId)"
CONFIG_SHA="$(python3 -c 'import hashlib,sys; h=hashlib.sha256(); [h.update(open(p,"rb").read()) for p in sys.argv[1:]]; print(h.hexdigest())' \
    "$CONFIG_SRC" "$UNIT_CONFIG_SRC" "$UNIT_MOVEMENT_SRC" "$UNIT_OPERATIONS_SRC")"
CONFIG_ID="$(printf '%s' "$CONFIG_SHA" | cut -c1-12)"
DEPLOYMENT_ID="$RELEASE_ID-cfg$CONFIG_ID"

case "$DEPLOYMENT_ID" in
    *[!A-Za-z0-9._-]*|"")
        echo "ERROR: unsafe deployment identifier: $DEPLOYMENT_ID" >&2
        exit 1
        ;;
esac

UPLOAD_DIR="/root/unitelabs-flex-upload-$DEPLOYMENT_ID-$$"
RELEASE_PATH="$STATE_DIR/releases/$DEPLOYMENT_ID"

echo "=== Flex connector staged deployment ==="
echo "Host:       $HOST"
echo "Artifact:   $RELEASE_ID"
echo "Config:     $CONFIG_ID"
echo "Install:    $RELEASE_PATH"
echo "Active:     $ACTIVE_PATH"

ssh "root@$HOST" "test ! -e '$UPLOAD_DIR' && mkdir -p '$UPLOAD_DIR'"
scp -O \
    "$MANIFEST_TOOL" \
    "$SCRIPT_DIR/scripts/install.sh" \
    "$ARTIFACT_DIR"/*.whl \
    "$ARTIFACT_DIR/runtime-manifest.json" \
    "$ARTIFACT_DIR/SHA256SUMS" \
    "root@$HOST:$UPLOAD_DIR/"
scp -O "$CONFIG_SRC" "root@$HOST:$UPLOAD_DIR/flex_config.json"
scp -O \
    "$UNIT_CONFIG_SRC" \
    "$UNIT_MOVEMENT_SRC" \
    "$UNIT_OPERATIONS_SRC" \
    "root@$HOST:$UPLOAD_DIR/"

ssh "root@$HOST" sh -s -- "$UPLOAD_DIR" "$RELEASE_PATH" "$ACTIVE_PATH" "$STATE_DIR" "$CONFIG_SHA" \
    "$CONNECTOR_VERSION" "$OPENTRONS_VERSION" "$OPENTRONS_SOURCE_COMMIT" "$PYTHON_VERSION" "$ARCHITECTURE" <<'REMOTE'
set -eu
UPLOAD_DIR="$1"
RELEASE_PATH="$2"
ACTIVE_PATH="$3"
STATE_DIR="$4"
EXPECTED_CONFIG_SHA="$5"
CONNECTOR_VERSION="$6"
OPENTRONS_VERSION="$7"
OPENTRONS_SOURCE_COMMIT="$8"
PYTHON_VERSION="$9"
ARCHITECTURE="${10}"

python3 "$UPLOAD_DIR/artifact_manifest.py" verify "$UPLOAD_DIR" \
    --connector-version "$CONNECTOR_VERSION" \
    --opentrons-version "$OPENTRONS_VERSION" \
    --robot-server-version "$OPENTRONS_VERSION" \
    --opentrons-source-commit "$OPENTRONS_SOURCE_COMMIT" \
    --python-version "$PYTHON_VERSION" \
    --architecture "$ARCHITECTURE" \
    --check-host-python \
    --check-host-architecture
UPLOADED_CONFIG_SHA="$(python3 -c 'import hashlib,sys; h=hashlib.sha256(); [h.update(open(p,"rb").read()) for p in sys.argv[1:]]; print(h.hexdigest())' \
    "$UPLOAD_DIR/flex_config.json" \
    "$UPLOAD_DIR/flex_unit_operations.json" \
    "$UPLOAD_DIR/asms_unit_labware_movement.json" \
    "$UPLOAD_DIR/asms_unit_operations.json")"
if [ "$UPLOADED_CONFIG_SHA" != "$EXPECTED_CONFIG_SHA" ]; then
    echo "ERROR: uploaded Flex configuration checksum mismatch." >&2
    exit 1
fi

if systemctl is-active --quiet sila2-connector 2>/dev/null; then
    echo "ERROR: sila2-connector is running. Switch to opentrons mode before deployment." >&2
    exit 1
fi

mount -o remount,rw /
mkdir -p "$STATE_DIR/releases"
LOCK_DIR="$STATE_DIR/operation.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "ERROR: another Flex deployment or mode transition is in progress ($LOCK_DIR)." >&2
    exit 1
fi
CLEANUP_INCOMPLETE=no
COMPLETE_MARKER="$RELEASE_PATH/.unitelabs-release-complete"
cleanup_deploy() {
    case "$UPLOAD_DIR" in
        /root/unitelabs-flex-upload-*) rm -rf "$UPLOAD_DIR" ;;
    esac
    if [ "$CLEANUP_INCOMPLETE" = yes ] &&
        [ -d "$RELEASE_PATH" ] &&
        [ ! -f "$COMPLETE_MARKER" ]; then
        rm -rf "$RELEASE_PATH"
    fi
    rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup_deploy 0 HUP INT TERM

if [ ! -f "$STATE_DIR/run-mutation.env" ] && [ -f "$ACTIVE_PATH/run-mutation.env" ]; then
    cp "$ACTIVE_PATH/run-mutation.env" "$STATE_DIR/run-mutation.env"
    chmod 600 "$STATE_DIR/run-mutation.env"
    echo "Migrated the existing mutation credential outside the release directory."
fi
if [ -f "$STATE_DIR/run-mutation.env" ]; then
    set -a
    . "$STATE_DIR/run-mutation.env"
    set +a
fi

if [ -e "$RELEASE_PATH" ] && {
    [ ! -f "$COMPLETE_MARKER" ] ||
    [ "$(cat "$COMPLETE_MARKER")" != "$EXPECTED_CONFIG_SHA" ];
}; then
    if [ -L "$ACTIVE_PATH" ] && [ "$(readlink -f "$ACTIVE_PATH")" = "$RELEASE_PATH" ]; then
        echo "ERROR: active release has no valid completion marker; roll back before repairing it." >&2
        exit 1
    fi
    echo "Removing an incomplete, never-activated release at $RELEASE_PATH."
    rm -rf "$RELEASE_PATH"
fi

if [ ! -e "$RELEASE_PATH" ]; then
    CLEANUP_INCOMPLETE=yes
    sh "$UPLOAD_DIR/install.sh" "$RELEASE_PATH"
    "$RELEASE_PATH/bin/python" -m unitelabs.opentrons_flex.runtime_preflight \
        --config "$RELEASE_PATH/config.json" \
        --require-robot-server \
        --require-mutation \
        --require-live-hardware
    "$RELEASE_PATH/bin/python" -m unitelabs.opentrons_flex.runtime_preflight \
        --config "$RELEASE_PATH/unit-config.json" \
        --require-sila-only \
        --require-live-hardware
    "$RELEASE_PATH/bin/python" -c \
        "from unitelabs.opentrons_flex.asms_unit_operations import load_manifest; from unitelabs.opentrons_flex.io import load_labware_movement_config; load_manifest('$RELEASE_PATH/asms-unit-operations.json'); load_labware_movement_config('$RELEASE_PATH/asms-unit-labware-movement.json')"
    printf '%s\n' "$EXPECTED_CONFIG_SHA" > "$COMPLETE_MARKER"
    CLEANUP_INCOMPLETE=no
else
    echo "Release already installed and complete; reusing $RELEASE_PATH"
    INSTALLED_CONFIG_SHA="$(python3 -c 'import hashlib,sys; h=hashlib.sha256(); [h.update(open(p,"rb").read()) for p in sys.argv[1:]]; print(h.hexdigest())' \
        "$RELEASE_PATH/config.json" \
        "$RELEASE_PATH/unit-config.json" \
        "$RELEASE_PATH/asms-unit-labware-movement.json" \
        "$RELEASE_PATH/asms-unit-operations.json")"
    if [ "$INSTALLED_CONFIG_SHA" != "$EXPECTED_CONFIG_SHA" ]; then
        echo "ERROR: immutable release configuration checksum mismatch." >&2
        exit 1
    fi
    if ! cmp -s "$RELEASE_PATH/runtime-manifest.json" "$UPLOAD_DIR/runtime-manifest.json"; then
        echo "ERROR: immutable release manifest differs from the verified artifact." >&2
        exit 1
    fi
fi

# This imports the exact HTTP and mutation symbols without touching the CAN bus.
"$RELEASE_PATH/bin/python" -m unitelabs.opentrons_flex.runtime_preflight \
    --config "$RELEASE_PATH/config.json" \
    --require-robot-server \
    --require-mutation \
    --require-live-hardware
"$RELEASE_PATH/bin/python" -m unitelabs.opentrons_flex.runtime_preflight \
    --config "$RELEASE_PATH/unit-config.json" \
    --require-sila-only \
    --require-live-hardware

if [ -L "$ACTIVE_PATH" ]; then
    PREVIOUS_TARGET="$(readlink -f "$ACTIVE_PATH")"
    if [ "$PREVIOUS_TARGET" = "$RELEASE_PATH" ]; then
        echo "Release is already active."
    else
        printf 'release:%s\n' "$PREVIOUS_TARGET" > "$STATE_DIR/previous-release"
        ln -sfn "$RELEASE_PATH" "$ACTIVE_PATH"
    fi
elif [ -d "$ACTIVE_PATH" ]; then
    LEGACY_PATH="$STATE_DIR/legacy-pre-versioned-release"
    if [ -e "$LEGACY_PATH" ]; then
        echo "ERROR: cannot preserve legacy release because $LEGACY_PATH already exists." >&2
        exit 1
    fi
    mv "$ACTIVE_PATH" "$LEGACY_PATH"
    printf 'legacy:%s\n' "$LEGACY_PATH" > "$STATE_DIR/previous-release"
    ln -s "$RELEASE_PATH" "$ACTIVE_PATH"
elif [ -e "$ACTIVE_PATH" ]; then
    echo "ERROR: active path exists but is neither a directory nor symlink: $ACTIVE_PATH" >&2
    exit 1
else
    ln -s "$RELEASE_PATH" "$ACTIVE_PATH"
fi

echo "Activated release: $(readlink -f "$ACTIVE_PATH")"

# Keep the active release and the rollback target; older releases only use disk
# space (each is a complete virtual environment).
KEEP_ACTIVE="$(readlink -f "$ACTIVE_PATH")"
KEEP_PREVIOUS=""
if [ -f "$STATE_DIR/previous-release" ]; then
    KEEP_PREVIOUS="$(sed -n '1s/^release://p' "$STATE_DIR/previous-release")"
fi
for old_release in "$STATE_DIR"/releases/*; do
    [ -d "$old_release" ] || continue
    case "$old_release" in
        "$KEEP_ACTIVE" | "$KEEP_PREVIOUS" | "$RELEASE_PATH") continue ;;
    esac
    echo "Removing old release $old_release"
    rm -rf "$old_release"
done
# Upload folders left behind by interrupted earlier deployments.
for stale_upload in /root/unitelabs-flex-upload-*; do
    if [ -d "$stale_upload" ] && [ "$stale_upload" != "$UPLOAD_DIR" ]; then
        rm -rf "$stale_upload"
    fi
done
df -h "$STATE_DIR" | sed -n '2s/^/Disk after cleanup: /p'

echo "Stock robot-server remains in control until the connector service is explicitly installed/switched."
rm -rf "$UPLOAD_DIR"
rmdir "$LOCK_DIR"
trap - 0 HUP INT TERM
REMOTE

echo "=== Deploy and no-hardware runtime preflight complete ==="
echo "Next: sh scripts/install_connector_service.sh $HOST"
