#!/bin/sh
# Verify, install, preflight, and atomically activate a Flex connector release.
set -eu

HOST="${1:-opentrons-flex}"
WHEEL_DIR="${2:-dist_arm}"
STATE_DIR="/var/lib/unitelabs-opentrons-flex"
ACTIVE_PATH="/var/sila2_flex"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
ARTIFACT_DIR="$SCRIPT_DIR/$WHEEL_DIR"
MANIFEST_TOOL="$SCRIPT_DIR/scripts/artifact_manifest.py"

if [ -f "$SCRIPT_DIR/config/flex_config.local.json" ]; then
    CONFIG_SRC="$SCRIPT_DIR/config/flex_config.local.json"
    echo "Config: config/flex_config.local.json (local override)"
else
    CONFIG_SRC="$SCRIPT_DIR/config/flex_config.json"
fi

if [ ! -d "$ARTIFACT_DIR" ]; then
    echo "ERROR: wheel directory '$ARTIFACT_DIR' not found." >&2
    echo "Download and extract the flex-arm-wheels artifact first." >&2
    exit 1
fi

python3 "$MANIFEST_TOOL" verify "$ARTIFACT_DIR" \
    --connector-version 0.9.1 \
    --opentrons-version 9.0.0 \
    --robot-server-version 9.0.0 \
    --opentrons-source-commit 44b37a2f91520bf2e7245c70bf799d46c8c2d9a5 \
    --python-version 3.10 \
    --architecture aarch64
RELEASE_ID="$(python3 "$MANIFEST_TOOL" field "$ARTIFACT_DIR" releaseId)"
CONFIG_SHA="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$CONFIG_SRC")"
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

ssh "root@$HOST" sh -s -- "$UPLOAD_DIR" "$RELEASE_PATH" "$ACTIVE_PATH" "$STATE_DIR" "$CONFIG_SHA" <<'REMOTE'
set -eu
UPLOAD_DIR="$1"
RELEASE_PATH="$2"
ACTIVE_PATH="$3"
STATE_DIR="$4"
EXPECTED_CONFIG_SHA="$5"

python3 "$UPLOAD_DIR/artifact_manifest.py" verify "$UPLOAD_DIR" \
    --connector-version 0.9.1 \
    --opentrons-version 9.0.0 \
    --robot-server-version 9.0.0 \
    --opentrons-source-commit 44b37a2f91520bf2e7245c70bf799d46c8c2d9a5 \
    --python-version 3.10 \
    --architecture aarch64 \
    --check-host-python \
    --check-host-architecture
UPLOADED_CONFIG_SHA="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$UPLOAD_DIR/flex_config.json")"
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
    printf '%s\n' "$EXPECTED_CONFIG_SHA" > "$COMPLETE_MARKER"
    CLEANUP_INCOMPLETE=no
else
    echo "Release already installed and complete; reusing $RELEASE_PATH"
    INSTALLED_CONFIG_SHA="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$RELEASE_PATH/config.json")"
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
echo "Stock robot-server remains in control until the connector service is explicitly installed/switched."
rmdir "$LOCK_DIR"
trap - 0 HUP INT TERM
REMOTE

echo "=== Deploy and no-hardware runtime preflight complete ==="
echo "Next: sh scripts/install_connector_service.sh $HOST"
