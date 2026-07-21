"""Console entrypoint for a local Unitelabs Flex acceptance run."""

import argparse
import asyncio
import json
from pathlib import Path

from ._helpers import INSTRUMENT_NAME
from .workflow import flex_system_acceptance_flow


def main() -> None:
    """Load the local manifest before starting the Unitelabs workflow."""
    parser = argparse.ArgumentParser(description="Run the guarded Flex system acceptance workflow.")
    parser.add_argument("--manifest", required=True, type=Path, help="Path to the completed acceptance JSON manifest.")
    parser.add_argument("--device", default=INSTRUMENT_NAME, help="Unitelabs service name.")
    arguments = parser.parse_args()
    manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
    asyncio.run(flex_system_acceptance_flow(manifest=manifest, device_name=arguments.device))


if __name__ == "__main__":
    main()
