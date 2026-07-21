"""Guardrails for simulator and real-hardware connector configs."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SMOKETEST_CONFIG = ROOT / "config" / "smoketest_config.json"
FLEX_CONFIG = ROOT / "config" / "flex_config.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def test_smoketest_config_never_targets_real_hardware() -> None:
    """The local smoketest config must run only against the OT3 simulator."""
    cfg = _load(SMOKETEST_CONFIG)

    assert cfg["use_simulator"] is True
    assert cfg["simulated_heater_shaker"] is True
    assert cfg["simulated_flex_stacker"] is True
    assert cfg["simulated_absorbance_reader"] is True
    assert cfg["simulated_temperature_module"] is True
    assert cfg["simulated_thermocycler"] is True
    assert cfg["with_robot_server"] is True
    assert cfg["sila_server"]["hostname"] in {"127.0.0.1", "localhost"}
    assert cfg["cloud_server_endpoint"] is None
    assert cfg["discovery"] is None


def test_real_flex_config_stays_explicitly_live() -> None:
    """The deploy config must not be silently converted to smoketest mode."""
    cfg = _load(FLEX_CONFIG)

    assert cfg["use_simulator"] is False
    assert cfg["simulated_heater_shaker"] is False
    assert cfg["simulated_flex_stacker"] is False
    assert cfg["simulated_absorbance_reader"] is False
    assert cfg["simulated_temperature_module"] is False
    assert cfg["simulated_thermocycler"] is False
    assert cfg["with_robot_server"] is True
    assert cfg["sila_server"]["hostname"] == "0.0.0.0"
