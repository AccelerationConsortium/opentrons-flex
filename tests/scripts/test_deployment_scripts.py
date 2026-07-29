from __future__ import annotations

import os
import hashlib
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _text(path: str) -> str:
    return (ROOT / path).read_text()


def _remote_body(path: str, runtime_root: Path) -> str:
    script = _text(path)
    marker = "<<'REMOTE'\n"
    start = script.index(marker) + len(marker)
    end = script.rindex("\nREMOTE")
    body = script[start:end] + "\n"
    replacements = {
        'STATE_DIR="/var/lib/unitelabs-opentrons-flex"': (
            f'STATE_DIR="{runtime_root}/var/lib/unitelabs-opentrons-flex"'
        ),
        'ACTIVE_PATH="/var/sila2_flex"': f'ACTIVE_PATH="{runtime_root}/var/sila2_flex"',
    }
    for original, isolated in replacements.items():
        if original not in body and original.startswith("ACTIVE_PATH="):
            continue
        assert original in body, f"{path} no longer exposes the expected isolated path seam"
        body = body.replace(original, isolated)
    systemd_unit = "cat > /etc/systemd/system/sila2-connector.service"
    if path == "scripts/install_connector_service.sh":
        assert systemd_unit in body, f"{path} no longer exposes the expected isolated systemd seam"
    if systemd_unit in body:
        body = body.replace(
            systemd_unit,
            f'cat > "{runtime_root}/etc/systemd/system/sila2-connector.service"',
        )
    assert 'STATE_DIR="/var/lib/unitelabs-opentrons-flex"' not in body
    assert 'ACTIVE_PATH="/var/sila2_flex"' not in body
    assert systemd_unit not in body
    return body


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents)
    path.chmod(0o755)


def _offline_remote_environment(tmp_path: Path) -> tuple[dict[str, str], dict[str, Path]]:
    runtime_root = tmp_path / "runtime-root"
    state_dir = runtime_root / "var/lib/unitelabs-opentrons-flex"
    releases_dir = state_dir / "releases"
    current_release = releases_dir / "current"
    previous_release = releases_dir / "previous"
    active_path = runtime_root / "var/sila2_flex"
    systemd_dir = runtime_root / "etc/systemd/system"
    fake_bin = tmp_path / "fake-bin"
    command_log = tmp_path / "commands.log"
    connector_active = tmp_path / "connector-active"
    stock_active = tmp_path / "stock-active"
    stock_enabled = tmp_path / "stock-enabled"

    for directory in (
        current_release / "bin",
        previous_release / "bin",
        active_path.parent,
        systemd_dir,
        fake_bin,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    active_path.symlink_to(current_release, target_is_directory=True)
    stock_active.touch()
    stock_enabled.touch()
    (state_dir / "run-mutation.env").write_text(
        "UNITELABS_RUN_MUTATION_TOKEN=" + ("t" * 32) + "\nUNITELABS_RUN_MUTATION_ACTOR=offline-harness\n"
    )
    (state_dir / "stock-service-state").write_text("opentrons-robot-server|yes|yes\n")
    for release in (current_release, previous_release):
        (release / "config.json").write_text("{}\n")
        (release / "unit-config.json").write_text("{}\n")
        (release / "asms-unit-labware-movement.json").write_text("{}\n")
        (release / "asms-unit-operations.json").write_text("{}\n")
        config_digest = hashlib.sha256(b"{}\n" * 4).hexdigest()
        (release / ".unitelabs-release-complete").write_text(config_digest + "\n")
        _write_executable(
            release / "bin/python",
            """#!/bin/sh
printf '%s\n' "release-python $*" >> "$FAKE_COMMAND_LOG"
exit "${FAKE_PREFLIGHT_EXIT:-0}"
""",
        )

    _write_executable(
        fake_bin / "systemctl",
        """#!/bin/sh
set -eu
printf '%s\n' "systemctl $*" >> "$FAKE_COMMAND_LOG"
command="${1:-}"
service="${2:-}"
if [ "$command" = "is-active" ] || [ "$command" = "is-enabled" ]; then
    for argument in "$@"; do
        service="$argument"
    done
fi
case "$command" in
    cat)
        selector="$FAKE_RUNTIME_ROOT/var/lib/unitelabs-opentrons-flex/connector-config.json"
        printf 'ExecStart=connector --config-path %s\n' "$selector"
        exit 0
        ;;
    is-enabled)
        case "$service" in
            sila2-connector) exit 1 ;;
            *) [ -f "$FAKE_STOCK_ENABLED" ] ;;
        esac
        ;;
    is-active)
        case "$service" in
            sila2-connector) [ -f "$FAKE_CONNECTOR_ACTIVE" ] ;;
            opentrons-robot-server) [ -f "$FAKE_STOCK_ACTIVE" ] ;;
            *) [ -f "$FAKE_STOCK_ACTIVE" ] ;;
        esac
        exit $?
        ;;
    start)
        if [ "$service" = "sila2-connector" ]; then
            [ "${FAKE_CONNECTOR_START_FAIL:-0}" != 1 ] || exit 1
            : > "$FAKE_CONNECTOR_ACTIVE"
        elif [ "$service" = "opentrons-robot-server" ]; then
            : > "$FAKE_STOCK_ACTIVE"
        fi
        ;;
    enable)
        if [ "$service" != "sila2-connector" ]; then
            : > "$FAKE_STOCK_ENABLED"
        fi
        ;;
    disable)
        if [ "$service" != "sila2-connector" ]; then
            rm -f "$FAKE_STOCK_ENABLED"
        fi
        ;;
    stop)
        if [ "$service" = "sila2-connector" ]; then
            rm -f "$FAKE_CONNECTOR_ACTIVE"
        elif [ "$service" = "opentrons-robot-server" ]; then
            [ "${FAKE_STOCK_STOP_FAIL:-0}" != 1 ] || exit 1
            rm -f "$FAKE_STOCK_ACTIVE"
        fi
        ;;
esac
exit 0
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/bin/sh
set -eu
url=""
for argument in "$@"; do
    url="$argument"
done
printf '%s\n' "curl $url" >> "$FAKE_COMMAND_LOG"
case "$url" in
    */openapi.json)
        [ -f "$FAKE_CONNECTOR_ACTIVE" ] && [ "${FAKE_CONNECTOR_HEALTH:-0}" = 1 ] || exit 1
        printf '%s\n' '{"paths":{"/unitelabs/runs/{run_id}/mutations":{}}}'
        ;;
    */deck_configuration)
        [ -f "$FAKE_CONNECTOR_ACTIVE" ] && [ "${FAKE_CONNECTOR_HEALTH:-0}" = 1 ]
        ;;
    */health)
        if [ -f "$FAKE_CONNECTOR_ACTIVE" ]; then
            selector="$FAKE_RUNTIME_ROOT/var/lib/unitelabs-opentrons-flex/connector-config.json"
            case "$(readlink "$selector" 2>/dev/null || true)" in
                */unit-config.json) [ "${FAKE_UNIT_HTTP_OPEN:-0}" = 1 ] ;;
                *) [ "${FAKE_CONNECTOR_HEALTH:-0}" = 1 ] ;;
            esac
        else
            [ -f "$FAKE_STOCK_ACTIVE" ] && [ "${FAKE_STOCK_HEALTH:-1}" = 1 ]
        fi
        ;;
    *)
        exit 1
        ;;
esac
""",
    )
    _write_executable(
        fake_bin / "python3",
        """#!/bin/sh
set -eu
printf '%s\n' "python3 $*" >> "$FAKE_COMMAND_LOG"
code="${2:-}"
case "$code" in
    *31950*)
        [ "${FAKE_HTTP_31950_OPEN:-0}" = 1 ]
        ;;
    *socket.create_connection*)
        [ -f "$FAKE_CONNECTOR_ACTIVE" ] && [ "${FAKE_CONNECTOR_HEALTH:-0}" = 1 ]
        ;;
    *json.load*)
        cat >/dev/null
        ;;
    *os.replace*)
        [ "${FAKE_ACTIVATION_FAIL:-0}" != 1 ] || exit 1
        rm -f "$4"
        mv "$3" "$4"
        ;;
    *hashlib*)
        printf '%s\n' "$FAKE_CONFIG_SHA"
        ;;
    *)
        exit 1
        ;;
esac
""",
    )
    for command in ("mount", "sleep", "journalctl"):
        _write_executable(
            fake_bin / command,
            f"""#!/bin/sh
printf '%s\\n' "{command} $*" >> "$FAKE_COMMAND_LOG"
exit 0
""",
        )

    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_RUNTIME_ROOT": str(runtime_root),
        "FAKE_COMMAND_LOG": str(command_log),
        "FAKE_CONNECTOR_ACTIVE": str(connector_active),
        "FAKE_STOCK_ACTIVE": str(stock_active),
        "FAKE_STOCK_ENABLED": str(stock_enabled),
        "FAKE_CONNECTOR_HEALTH": "1",
        "FAKE_STOCK_HEALTH": "1",
        "FAKE_PREFLIGHT_EXIT": "0",
        "FAKE_ACTIVATION_FAIL": "0",
        "FAKE_CONFIG_SHA": config_digest,
        "FAKE_STOCK_STOP_FAIL": "0",
        "FAKE_UNIT_HTTP_OPEN": "0",
    }
    paths = {
        "active": active_path,
        "command_log": command_log,
        "connector_active": connector_active,
        "current_release": current_release,
        "previous_release": previous_release,
        "previous_record": state_dir / "previous-release",
        "config_selector": state_dir / "connector-config.json",
        "stock_active": stock_active,
        "stock_enabled": stock_enabled,
    }
    return environment, paths


def _run_remote_body(
    path: str,
    environment: dict[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    runtime_root = Path(environment["FAKE_RUNTIME_ROOT"])
    return subprocess.run(
        ["sh", "-s", "--", *arguments],
        input=_remote_body(path, runtime_root),
        text=True,
        capture_output=True,
        check=False,
        env=environment,
        timeout=10,
    )


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
        "scripts/reconcile_asms_unit_deck.sh",
    ):
        script = _text(path)
        assert 'LOCK_DIR="$STATE_DIR/operation.lock"' in script
        assert 'mkdir "$LOCK_DIR"' in script
        assert "trap " in script


def test_unit_deck_reconciliation_is_explicit_audited_and_recoverable() -> None:
    script = _text("scripts/reconcile_asms_unit_deck.sh")

    assert "ASMS-UNIT-RECONCILE-A1-B1-B2-EMPTY" in script
    assert "systemctl is-active --quiet sila2-connector" in script
    assert "asms-unit-deck-reconciliations.jsonl" in script
    assert 'cp -p "$LEDGER" "$BACKUP"' in script
    assert 'mv "$TEMP_LEDGER" "$LEDGER"' in script
    assert script.index('mkdir "$LOCK_DIR"') < script.index("systemctl is-active --quiet sila2-connector")
    assert script.index('"action":"authorized-reset-to-A1-B1"') < script.index('mv "$TEMP_LEDGER" "$LEDGER"')
    assert 'rm "$LEDGER"' not in script


def test_unit_deck_reconciliation_preserves_malformed_ledger_before_reset(tmp_path: Path) -> None:
    environment, _paths = _offline_remote_environment(tmp_path)
    runtime_root = Path(environment["FAKE_RUNTIME_ROOT"])
    state_dir = runtime_root / "var/lib/unitelabs-opentrons-flex"
    ledger = state_dir / "asms-unit-deck-state.json"
    ledger.write_text("{truncated")
    fake_bin = Path(environment["PATH"].split(os.pathsep)[0])
    _write_executable(fake_bin / "python3", "#!/bin/sh\nexit 1\n")

    result = _run_remote_body(
        "scripts/reconcile_asms_unit_deck.sh",
        environment,
        "offline-operator",
        "ASMS-UNIT-RECONCILE-A1-B1-B2-EMPTY",
    )

    assert result.returncode == 0, result.stderr
    backups = list(state_dir.glob("asms-unit-deck-state.json.reconciled-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "{truncated"
    assert '"valid":true' in ledger.read_text()
    assert '"action":"authorized-reset-to-A1-B1"' in (state_dir / "asms-unit-deck-reconciliations.jsonl").read_text()


def test_install_inherits_only_robot_image_bindings_and_overrides_pip_root() -> None:
    script = _text("scripts/install.sh")

    assert "venv --system-site-packages" in script
    assert 'pip" install --root / --no-index --no-deps' in script
    assert "flex_unit_operations.json" in script
    assert "asms_unit_labware_movement.json" in script
    assert "asms_unit_operations.json" in script


def test_connector_unit_switch_uses_sila_only_config_and_health(tmp_path: Path) -> None:
    environment, paths = _offline_remote_environment(tmp_path)

    result = _run_remote_body("scripts/switch_mode.sh", environment, "connector-unit")
    command_log = paths["command_log"].read_text()

    assert result.returncode == 0, result.stderr
    assert paths["config_selector"].is_symlink()
    assert paths["config_selector"].resolve() == paths["current_release"] / "unit-config.json"
    assert "--require-sila-only --require-live-hardware" in command_log
    assert "asms_unit_operations health" in command_log
    assert paths["connector_active"].is_file()
    assert not paths["stock_active"].exists()
    assert not paths["stock_enabled"].exists()


def test_connector_unit_repairs_preexisting_dual_owner_state(tmp_path: Path) -> None:
    environment, paths = _offline_remote_environment(tmp_path)
    paths["connector_active"].touch()
    paths["config_selector"].symlink_to(paths["current_release"] / "unit-config.json")

    result = _run_remote_body("scripts/switch_mode.sh", environment, "connector-unit")

    assert result.returncode == 0, result.stderr
    assert "systemctl stop opentrons-robot-server" in paths["command_log"].read_text()
    assert paths["connector_active"].is_file()
    assert not paths["stock_active"].exists()


def test_connector_unit_refuses_when_stock_owner_cannot_stop(tmp_path: Path) -> None:
    environment, paths = _offline_remote_environment(tmp_path)
    environment["FAKE_STOCK_STOP_FAIL"] = "1"

    result = _run_remote_body("scripts/switch_mode.sh", environment, "connector-unit")

    assert result.returncode != 0
    assert "refusing dual OT3API ownership" in result.stderr
    assert not paths["connector_active"].exists()
    assert paths["stock_active"].is_file()


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


def test_service_install_preflight_failure_never_touches_service_state(tmp_path: Path) -> None:
    environment, paths = _offline_remote_environment(tmp_path)
    environment["FAKE_PREFLIGHT_EXIT"] = "1"

    result = _run_remote_body("scripts/install_connector_service.sh", environment)

    assert result.returncode != 0
    assert "systemctl " not in paths["command_log"].read_text()
    assert paths["stock_active"].is_file()
    assert not paths["connector_active"].exists()


def test_service_health_timeout_restores_stock_mode(tmp_path: Path) -> None:
    environment, paths = _offline_remote_environment(tmp_path)
    environment["FAKE_CONNECTOR_HEALTH"] = "0"

    result = _run_remote_body("scripts/install_connector_service.sh", environment)
    command_log = paths["command_log"].read_text()

    assert result.returncode != 0
    assert "connector failed the bounded startup health check" in result.stderr
    assert "systemctl start sila2-connector" in command_log
    assert "systemctl stop sila2-connector" in command_log
    assert "systemctl start opentrons-robot-server" in command_log
    assert paths["stock_active"].is_file()
    assert not paths["connector_active"].exists()


def test_rollback_activation_failure_keeps_current_release_active(tmp_path: Path) -> None:
    environment, paths = _offline_remote_environment(tmp_path)
    paths["previous_record"].write_text(f"release:{paths['previous_release']}\n")
    environment["FAKE_ACTIVATION_FAIL"] = "1"

    result = _run_remote_body("scripts/rollback_connector.sh", environment)

    assert result.returncode != 0
    assert paths["active"].resolve() == paths["current_release"]
    assert paths["previous_record"].read_text() == f"release:{paths['previous_release']}\n"
    assert paths["stock_active"].is_file()


def test_rollback_rejects_release_without_completion_marker(tmp_path: Path) -> None:
    environment, paths = _offline_remote_environment(tmp_path)
    paths["previous_record"].write_text(f"release:{paths['previous_release']}\n")
    (paths["previous_release"] / ".unitelabs-release-complete").unlink()

    result = _run_remote_body("scripts/rollback_connector.sh", environment)

    assert result.returncode != 0
    assert "no deployment completion marker" in result.stderr
    assert paths["active"].resolve() == paths["current_release"]


def test_rollback_rejects_release_with_changed_configuration(tmp_path: Path) -> None:
    environment, paths = _offline_remote_environment(tmp_path)
    paths["previous_record"].write_text(f"release:{paths['previous_release']}\n")
    environment["FAKE_CONFIG_SHA"] = "0" * 64

    result = _run_remote_body("scripts/rollback_connector.sh", environment)

    assert result.returncode != 0
    assert "configuration checksum mismatch" in result.stderr
    assert paths["active"].resolve() == paths["current_release"]


def test_rollback_atomically_swaps_release_and_records_prior_target(tmp_path: Path) -> None:
    environment, paths = _offline_remote_environment(tmp_path)
    paths["previous_record"].write_text(f"release:{paths['previous_release']}\n")

    result = _run_remote_body("scripts/rollback_connector.sh", environment)

    assert result.returncode == 0, result.stderr
    assert paths["active"].resolve() == paths["previous_release"]
    assert paths["previous_record"].read_text() == f"release:{paths['current_release']}\n"
    assert paths["stock_active"].is_file()
