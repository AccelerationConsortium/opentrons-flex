from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _text(path: str) -> str:
    return (ROOT / path).read_text()


def test_deploy_verifies_artifact_and_runtime_before_activation() -> None:
    script = _text("deploy.sh")

    local_verify = script.index('python3 "$MANIFEST_TOOL" verify')
    upload = script.index("scp -O")
    runtime_preflight = script.index("unitelabs.opentrons_flex.runtime_preflight")
    activate = script.index('ln -sfn "$RELEASE_PATH" "$ACTIVE_PATH"')

    assert local_verify < upload
    assert runtime_preflight < activate
    assert "--check-host-python" in script
    assert "--check-host-architecture" in script
    assert "--require-live-hardware" in script
    assert 'COMPLETE_MARKER="$RELEASE_PATH/.unitelabs-release-complete"' in script
    assert "Removing an incomplete, never-activated release" in script
    assert "rm -rf $VENV_PATH" not in script


def test_service_switch_has_bounded_health_and_stock_failover() -> None:
    script = _text("scripts/install_connector_service.sh")

    preflight = script.index("unitelabs.opentrons_flex.runtime_preflight")
    stop_stock = script.index('echo "Stopping stock hardware owners..."')

    assert preflight < stop_stock
    assert "restore_stock" in script
    assert "Stock Opentrons mode restored and healthy." in script
    assert "--max-time 10" in script
    assert "/deck_configuration" in script
    assert "/unitelabs/runs/{run_id}/mutations" in script


def test_secrets_and_ledgers_live_outside_immutable_release() -> None:
    for path in ("deploy.sh", "scripts/install_connector_service.sh", "scripts/switch_mode.sh"):
        script = _text(path)
        assert "/var/lib/unitelabs-opentrons-flex/run-mutation.env" in script or (
            'STATE_DIR="/var/lib/unitelabs-opentrons-flex"' in script
            and ('ENV_FILE="$STATE_DIR/run-mutation.env"' in script or '"$STATE_DIR/run-mutation.env"' in script)
        )


def test_all_state_transitions_share_a_fail_closed_operation_lock() -> None:
    for path in (
        "deploy.sh",
        "scripts/install_connector_service.sh",
        "scripts/switch_mode.sh",
        "scripts/rollback_connector.sh",
    ):
        script = _text(path)
        assert 'LOCK_DIR="$STATE_DIR/operation.lock"' in script
        assert 'mkdir "$LOCK_DIR"' in script
        assert "trap " in script


def test_install_inherits_only_robot_image_bindings_and_overrides_pip_root() -> None:
    script = _text("scripts/install.sh")

    assert "venv --system-site-packages" in script
    assert 'pip" install --root / --no-index --no-deps' in script


def test_rollback_leaves_stock_robot_server_active() -> None:
    script = _text("scripts/rollback_connector.sh")

    stop_connector = script.index("systemctl stop sila2-connector")
    restore_stock = script.rindex("restore_stock_services")
    activate_previous = script.rindex('activate_target "$TARGET"')

    assert stop_connector < restore_stock < activate_previous
    assert "systemctl start opentrons-robot-server" in script
    assert "stock-service-state" in script
    assert "stock robot-server did not recover within 120 seconds" in script
    assert "os.replace" in script
    assert "printf 'legacy:%s\\n' \"$CURRENT_TARGET\"" in script
