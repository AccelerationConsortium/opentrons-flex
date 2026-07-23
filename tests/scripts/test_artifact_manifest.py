from __future__ import annotations

import json
from hashlib import sha256

import pytest

from scripts import artifact_manifest
from unitelabs.opentrons_flex import runtime_compat


def _write_required_wheels(directory) -> None:
    names = [
        "unitelabs_opentrons_flex-0.9.1-py3-none-any.whl",
        "opentrons-9.0.0-py3-none-any.whl",
        "opentrons_shared_data-9.0.0-py3-none-any.whl",
        "opentrons_hardware-9.0.0-py3-none-any.whl",
        "server_utils-9.0.0-py3-none-any.whl",
        "robot_server-9.0.0-py3-none-any.whl",
    ]
    names.extend(
        f"{distribution.replace('-', '_')}-{version}-py3-none-any.whl"
        for distribution, version in artifact_manifest.PINNED_RUNTIME_WHEELS.items()
    )
    for index, name in enumerate(names):
        (directory / name).write_bytes(f"wheel-{index}".encode())


def _build(directory):
    return artifact_manifest.build_manifest(
        directory,
        connector_version="0.9.1",
        opentrons_version="9.0.0",
        robot_server_version="9.0.0",
        opentrons_source_commit="44b37a2f91520bf2e7245c70bf799d46c8c2d9a5",
        python_version="3.10",
        architecture="aarch64",
    )


def test_build_and_verify_exact_runtime_artifact(tmp_path) -> None:
    _write_required_wheels(tmp_path)

    manifest = _build(tmp_path)
    verified = artifact_manifest.verify_manifest(
        tmp_path,
        expected={
            "connectorVersion": "0.9.1",
            "opentronsVersion": "9.0.0",
            "robotServerVersion": "9.0.0",
            "opentronsSourceCommit": "44b37a2f91520bf2e7245c70bf799d46c8c2d9a5",
            "pythonVersion": "3.10",
            "architecture": "aarch64",
        },
        check_host_python=True,
        check_host_architecture=False,
    )

    assert verified == manifest
    assert manifest["releaseId"].startswith("flex-0.9.1-ot9.0.0-py3.10-aarch64-")
    assert json.loads((tmp_path / artifact_manifest.MANIFEST_NAME).read_text())["bundleSha256"]
    assert (tmp_path / artifact_manifest.CHECKSUMS_NAME).is_file()


def test_artifact_and_startup_runtime_contracts_cannot_drift() -> None:
    expected = {name.lower(): version for name, version in runtime_compat.SUPPORTED_RUNTIME_PACKAGES.items()}
    assert expected == artifact_manifest.PINNED_RUNTIME_WHEELS


def test_verify_rejects_tampered_wheel(tmp_path) -> None:
    _write_required_wheels(tmp_path)
    _build(tmp_path)
    (tmp_path / "opentrons-9.0.0-py3-none-any.whl").write_bytes(b"tampered")

    with pytest.raises(RuntimeError, match=r"Checksum verification failed|Manifest verification failed"):
        artifact_manifest.verify_manifest(
            tmp_path,
            expected={},
            check_host_python=False,
            check_host_architecture=False,
        )


def test_build_rejects_mixed_opentrons_runtime(tmp_path) -> None:
    _write_required_wheels(tmp_path)
    old_wheel = tmp_path / "opentrons-9.0.0-py3-none-any.whl"
    old_wheel.rename(tmp_path / "opentrons-8.8.1-py3-none-any.whl")

    with pytest.raises(RuntimeError, match=r"expected exactly 9\.0\.0"):
        _build(tmp_path)


def test_verify_rejects_manifest_that_omits_required_runtime_wheel(tmp_path) -> None:
    _write_required_wheels(tmp_path)
    _build(tmp_path)
    missing_name = "paho_mqtt-1.6.1-py3-none-any.whl"
    (tmp_path / missing_name).unlink()
    manifest_path = tmp_path / artifact_manifest.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest["wheels"] = [record for record in manifest["wheels"] if record["filename"] != missing_name]
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    checksum_path = tmp_path / artifact_manifest.CHECKSUMS_NAME
    checksum_lines = [
        line
        for line in checksum_path.read_text().splitlines()
        if not line.endswith(f"  {missing_name}") and not line.endswith(f"  {artifact_manifest.MANIFEST_NAME}")
    ]
    checksum_lines.append(f"{sha256(manifest_path.read_bytes()).hexdigest()}  {artifact_manifest.MANIFEST_NAME}")
    checksum_path.write_text("\n".join(checksum_lines) + "\n")

    with pytest.raises(RuntimeError, match=r"paho-mqtt wheel version is missing"):
        artifact_manifest.verify_manifest(
            tmp_path,
            expected={},
            check_host_python=False,
            check_host_architecture=False,
        )
