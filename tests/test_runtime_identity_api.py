from __future__ import annotations

from unitelabs.opentrons_flex import runtime_identity_api
from unitelabs.opentrons_flex.runtime_compat import RuntimeCompatibilityReport


def _compatibility() -> RuntimeCompatibilityReport:
    return RuntimeCompatibilityReport(
        connector_version="0.9.1",
        python_version="3.10.20",
        opentrons_version="9.0.0",
        robot_server_version="9.0.0",
        robot_server_source="/release/robot_server/__init__.py",
        runtime_package_versions={},
        runtime_contract_id="flex-runtime-test",
        private_api_checks=(),
        base_compatible=True,
        mutation_compatible=True,
        issues=(),
        mutation_issues=(),
    )


def test_process_identity_uses_prefix_captured_at_process_import(monkeypatch, tmp_path) -> None:
    release_a = tmp_path / "release-a"
    release_a.mkdir()
    release_b = tmp_path / "release-b"
    release_b.mkdir()
    active = tmp_path / "active"
    active.symlink_to(release_a, target_is_directory=True)
    captured_prefix = active.resolve()
    active.unlink()
    active.symlink_to(release_b, target_is_directory=True)
    monkeypatch.setattr(runtime_identity_api, "_PROCESS_RELEASE_PREFIX", captured_prefix)

    observed_prefixes = []

    def _release_identity_for_test(**kwargs):
        observed_prefixes.append(kwargs["prefix"])
        return (
            {
                "release_id": kwargs["prefix"].name,
                "bundle_sha256": "a" * 64,
            },
            (),
        )

    monkeypatch.setattr(
        runtime_identity_api,
        "_release_identity",
        _release_identity_for_test,
    )

    identity = runtime_identity_api.process_runtime_identity(
        _compatibility(),
        require_release=True,
    )

    assert observed_prefixes == [release_a]
    assert identity["releaseIdentity"]["release_id"] == "release-a"
