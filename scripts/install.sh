#!/bin/sh
# Install one immutable Flex connector release from the current artifact folder.
set -eu

ARTIFACT_DIR="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
RELEASE_PATH="${1:?Usage: install.sh <absolute-release-path>}"

case "$RELEASE_PATH" in
    /var/lib/unitelabs-opentrons-flex/releases/*) ;;
    *)
        echo "ERROR: release path must be under /var/lib/unitelabs-opentrons-flex/releases" >&2
        exit 1
        ;;
esac

if [ -e "$RELEASE_PATH" ]; then
    echo "ERROR: release path already exists: $RELEASE_PATH" >&2
    exit 1
fi

mkdir -p "$(dirname "$RELEASE_PATH")"
python3 -m venv --system-site-packages "$RELEASE_PATH"

# The robot image's /etc/pip.conf sets "root=/var/user-packages". --root /
# is required even inside a venv or pip silently installs elsewhere.
"$RELEASE_PATH/bin/pip" install --root / --no-index --no-deps "$ARTIFACT_DIR"/*.whl
cp "$ARTIFACT_DIR/flex_config.json" "$RELEASE_PATH/config.json"
cp "$ARTIFACT_DIR/runtime-manifest.json" "$RELEASE_PATH/runtime-manifest.json"

echo "Installed immutable release at $RELEASE_PATH"
