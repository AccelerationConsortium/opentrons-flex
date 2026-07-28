from __future__ import annotations

import re
from pathlib import Path

import yaml

from scripts import artifact_manifest
from unitelabs.opentrons_flex import runtime_compat

_ROOT = Path(__file__).parents[1]


def _text(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


def _normalized_runtime_packages() -> dict[str, str]:
    return {
        re.sub(r"[-_.]+", "-", name).lower(): version
        for name, version in runtime_compat.SUPPORTED_RUNTIME_PACKAGES.items()
    }


def test_runtime_version_and_source_pin_are_consistent_across_release_surfaces() -> None:
    opentrons_version = runtime_compat.SUPPORTED_OPENTRONS_VERSION
    source_commit = runtime_compat.SUPPORTED_OPENTRONS_SOURCE_COMMIT
    python_version = ".".join(str(part) for part in runtime_compat.SUPPORTED_PYTHON_VERSION)
    pyproject = _text("pyproject.toml")

    assert f'"opentrons=={opentrons_version}"' in pyproject
    assert f'requires-python=">={python_version},<3.11"' in pyproject

    required_fragments = {
        "Dockerfile.build": (
            f"python{python_version}-bookworm",
            f"ARG OPENTRONS_VERSION={opentrons_version}",
            f"ARG OPENTRONS_COMMIT={source_commit}",
        ),
        "deploy.sh": (
            f"--opentrons-version {opentrons_version}",
            f"--robot-server-version {opentrons_version}",
            f"--opentrons-source-commit {source_commit}",
            f"--python-version {python_version}",
        ),
        ".github/workflows/test.yml": (
            f'opentrons-version: "{opentrons_version}"',
            f'opentrons-commit: "{source_commit}"',
            f'python-version: "{python_version}"',
        ),
        ".github/workflows/build-flex-arm-wheels.yml": (
            f"--opentrons-version {opentrons_version}",
            f"--robot-server-version {opentrons_version}",
            f"--opentrons-source-commit {source_commit}",
            f"--python-version {python_version}",
        ),
    }
    for path, fragments in required_fragments.items():
        contents = _text(path)
        assert all(fragment in contents for fragment in fragments), f"{path} drifted from the runtime contract"


def test_runtime_dependency_manifest_matches_startup_contract() -> None:
    assert _normalized_runtime_packages() == artifact_manifest.PINNED_RUNTIME_WHEELS


def test_flex_documentation_does_not_claim_the_retired_runtime() -> None:
    for path in ("README.md", "docs/asms_flex_workflow_test.md"):
        assert "8.8.1" not in _text(path), f"{path} still claims the retired Flex runtime"


def test_arm_artifact_build_is_a_pr_gate_for_every_release_input() -> None:
    workflow = yaml.load(
        _text(".github/workflows/build-flex-arm-wheels.yml"),
        Loader=yaml.BaseLoader,
    )
    triggers = workflow["on"]
    required_paths = {
        ".github/workflows/build-flex-arm-wheels.yml",
        "Dockerfile.build",
        "packages/flex-acceptance-contract/**",
        "pyproject.toml",
        "scripts/artifact_manifest.py",
        "src/**",
        "uv.lock",
    }

    assert triggers["pull_request"]["branches"] == ["main"]
    assert required_paths <= set(triggers["pull_request"]["paths"])
    assert required_paths <= set(triggers["push"]["paths"])
