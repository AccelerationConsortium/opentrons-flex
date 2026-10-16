"""Console entrypoint for a local Unitelabs Flex acceptance run."""

import argparse
import asyncio
import json
from pathlib import Path

from unitelabs_flex_acceptance_contract import AcceptanceManifest

_INSTRUMENT_NAME = "Opentrons Flex"


def main() -> None:
    """Load the local manifest before starting the Unitelabs workflow."""
    parser = argparse.ArgumentParser(description="Run the guarded Flex system acceptance workflow.")
    parser.add_argument("--manifest", required=True, type=Path, help="Path to the completed acceptance JSON manifest.")
    parser.add_argument("--device", default=_INSTRUMENT_NAME, help="Unitelabs service name.")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the cross-runtime manifest and exit without connecting to Unitelabs or hardware.",
    )
    arguments = parser.parse_args()
    manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
    AcceptanceManifest.parse(manifest)
    if arguments.validate_only:
        return

    # Keep workflow-engine imports out of the cross-platform validation path so
    # Windows CI can exercise this exact console entrypoint without credentials.
    from .workflow import flex_system_acceptance_flow

    asyncio.run(flex_system_acceptance_flow(manifest=manifest, device_name=arguments.device))


if __name__ == "__main__":
    main()
