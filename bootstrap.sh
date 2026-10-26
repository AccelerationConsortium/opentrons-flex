#!/bin/sh
# One-step start for macOS and Linux: installs uv if needed, installs the
# connector and its tools, then runs the guided setup (simulator or real Flex).
#
#   ./bootstrap.sh                      # interactive
#   ./bootstrap.sh --host 10.0.0.42     # skip robot discovery
set -eu
cd "$(CDPATH= cd -- "$(dirname "$0")" && pwd)"

if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv (Python package manager)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
fi
for tool in ssh scp; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "WARNING: '$tool' (OpenSSH client) is needed to deploy to a robot; the simulator works without it." >&2
    fi
done

echo "Installing the connector and flexctl (Python 3.12)..."
uv sync --python 3.12 --all-extras
exec uv run flexctl setup "$@"
