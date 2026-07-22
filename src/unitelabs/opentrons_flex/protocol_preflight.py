"""
Offline preflight for Opentrons Python protocols used through robot-server.

The connector exposes Opentrons' ``/protocols`` and ``/runs`` HTTP surface in
parallel with SiLA.  This module validates a protocol before that HTTP path is
allowed anywhere near a real Flex.  It intentionally distinguishes an exact
simulation (all real labware definitions are present) from a shadow simulation
that substitutes compatible built-in definitions to exercise command logic.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from opentrons.protocol_api import MAX_SUPPORTED_VERSION
from opentrons.protocol_api.labware import get_labware_definition
from opentrons.simulate import simulate
from opentrons_shared_data.labware.labware_definition import labware_definition_type_adapter
from pydantic import ValidationError


@dataclass(frozen=True)
class ProtocolPreflightReport:
    """Machine-readable evidence from one offline protocol preflight."""

    protocol_path: str
    protocol_name: str
    api_level: str
    maximum_supported_api_level: str
    robot_type: str
    labware_load_names: tuple[str, ...]
    missing_labware: tuple[str, ...]
    shadow_substitutions: tuple[tuple[str, str], ...]
    simulation_completed: bool
    simulation_error: str | None
    custom_labware_identities_verified: bool
    command_count: int
    tip_pickups: int
    returned_tip_pickups: int
    discarded_tip_pickups: int
    gripper_moves: int
    aspirate_commands: int
    dispense_commands: int
    delay_seconds: float
    temperature_deactivations: int

    @property
    def exact_bundle_ready(self) -> bool:
        """Whether this evidence is sufficient to upload the real bundle."""
        return (
            self.robot_type == "Flex"
            and self.simulation_completed
            and not self.missing_labware
            and not self.shadow_substitutions
            and self.custom_labware_identities_verified
        )

    @property
    def shadow_passed(self) -> bool:
        """Whether logic ran with explicit non-production labware substitutions."""
        return self.simulation_completed and bool(self.shadow_substitutions)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation including readiness flags."""
        result = asdict(self)
        result["exact_bundle_ready"] = self.exact_bundle_ready
        result["shadow_passed"] = self.shadow_passed
        return result


def _literal_assignment(tree: ast.Module, name: str) -> object | None:
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        value = node.value
        if value is None:
            return None
        try:
            return ast.literal_eval(value)
        except (ValueError, TypeError):
            return None
    return None


def _string_argument(call: ast.Call, names: set[str]) -> str | None:
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    for keyword in call.keywords:
        if keyword.arg in names and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
    return None


def _labware_load_names(tree: ast.Module) -> tuple[str, ...]:
    result: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not isinstance(function, ast.Attribute) or function.attr != "load_labware":
            continue
        load_name = _string_argument(node, {"name", "load_name"})
        if load_name:
            result.add(load_name)
    return tuple(sorted(result))


def _custom_labware_hashes(paths: Sequence[Path]) -> dict[str, str]:
    result: dict[str, tuple[Path, str]] = {}
    for directory in paths:
        if not directory.is_dir():
            message = f"Custom labware directory does not exist: {directory}"
            raise ValueError(message)
        for definition_path in directory.glob("*.json"):
            try:
                definition = json.loads(definition_path.read_text(encoding="utf-8"))
                parsed = labware_definition_type_adapter.validate_python(definition)
                load_name = parsed.parameters.loadName
                canonical = json.dumps(
                    definition,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
                semantic_hash = hashlib.sha256(canonical).hexdigest()
            except (OSError, json.JSONDecodeError, ValidationError) as exc:
                message = f"Invalid custom labware definition {definition_path}: {exc}"
                raise ValueError(message) from exc
            existing = result.get(load_name)
            if existing is not None:
                message = f"Duplicate custom labware loadName {load_name!r}: {existing[0]} and {definition_path}."
                raise ValueError(message)
            result[load_name] = (definition_path, semantic_hash)
    return {load_name: item[1] for load_name, item in result.items()}


def _is_builtin_labware(load_name: str) -> bool:
    try:
        get_labware_definition(load_name)
    except (FileNotFoundError, KeyError, ValueError):
        return False
    return True


def _version_tuple(value: str) -> tuple[int, int]:
    parts = value.split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        message = f"Protocol API level must be MAJOR.MINOR, received {value!r}."
        raise ValueError(message)
    return int(parts[0]), int(parts[1])


def _prepare_shadow_source(source: str, substitutions: Mapping[str, str]) -> str:
    """Materialize non-deployable source for this module's offline simulator."""
    result = source
    for original, substitute in substitutions.items():
        result = result.replace(original, substitute)
    return result


def inspect_protocol(
    protocol_path: str | Path,
    *,
    custom_labware_paths: Sequence[str | Path] = (),
    shadow_labware: Mapping[str, str] | None = None,
    expected_custom_labware_hashes: Mapping[str, str] | None = None,
) -> ProtocolPreflightReport:
    """
    Statically inspect and, when possible, simulate one Flex protocol.

    Shadow substitutions are never evidence that the physical labware geometry
    is safe.  They exist only to exercise the protocol's command and tip logic
    while exact custom definitions are unavailable.
    """
    path = Path(protocol_path).expanduser().resolve()
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(path))
    raw_requirements = _literal_assignment(tree, "requirements")
    raw_metadata = _literal_assignment(tree, "metadata")
    requirements = raw_requirements if isinstance(raw_requirements, dict) else {}
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}

    api_level = str(requirements.get("apiLevel", ""))
    robot_type = str(requirements.get("robotType", ""))
    if not api_level:
        message = "Protocol requirements must declare apiLevel."
        raise ValueError(message)
    if robot_type != "Flex":
        message = f"Protocol requirements must declare robotType='Flex', received {robot_type!r}."
        raise ValueError(message)
    if _version_tuple(api_level) > (MAX_SUPPORTED_VERSION.major, MAX_SUPPORTED_VERSION.minor):
        message = f"Protocol API level {api_level} exceeds installed Opentrons support {MAX_SUPPORTED_VERSION}."
        raise ValueError(message)

    labware_paths = tuple(Path(item).expanduser().resolve() for item in custom_labware_paths)
    custom_hashes = _custom_labware_hashes(labware_paths)
    load_names = _labware_load_names(tree)
    expected_hashes = dict(expected_custom_labware_hashes or {})
    used_custom_names = tuple(name for name in load_names if name in custom_hashes)
    identities_verified = not used_custom_names or expected_custom_labware_hashes is not None
    if expected_custom_labware_hashes is not None:
        for load_name in load_names:
            actual_hash = custom_hashes.get(load_name)
            if actual_hash is None:
                continue
            expected_hash = expected_hashes.get(load_name)
            if expected_hash is None:
                message = f"Custom labware {load_name!r} has no approved identity for this protocol."
                raise ValueError(message)
            if actual_hash != expected_hash:
                message = (
                    f"Custom labware {load_name!r} identity mismatch: expected semantic SHA-256 "
                    f"{expected_hash}, received {actual_hash}."
                )
                raise ValueError(message)
    missing = tuple(name for name in load_names if name not in custom_hashes and not _is_builtin_labware(name))

    requested_shadow = dict(shadow_labware or {})
    applied_shadow = {name: requested_shadow[name] for name in missing if name in requested_shadow}
    unresolved = tuple(name for name in missing if name not in applied_shadow)
    simulation_source = _prepare_shadow_source(source, applied_shadow)

    simulation_completed = False
    simulation_error: str | None = None
    runlog: list[Mapping[str, Any]] = []
    if not unresolved:
        try:
            runlog, _ = simulate(
                StringIO(simulation_source),
                file_name=path.name,
                custom_labware_paths=[str(item) for item in labware_paths],
            )
            simulation_completed = True
        except Exception as exc:  # noqa: BLE001 - surface upstream protocol failures verbatim
            simulation_error = f"{type(exc).__name__}: {exc}"

    texts = [str(entry.get("payload", {}).get("text", "")) for entry in runlog]
    tip_pickups = sum(text.startswith("Picking up tip") for text in texts)
    returns = sum(text == "Returning tip" for text in texts)
    drop_commands = sum(text.startswith("Dropping tip") for text in texts)
    delay_seconds = sum(
        float(entry.get("payload", {}).get("minutes", 0)) * 60 + float(entry.get("payload", {}).get("seconds", 0))
        for entry in runlog
        if str(entry.get("payload", {}).get("text", "")).startswith("Delaying for")
    )

    return ProtocolPreflightReport(
        protocol_path=str(path),
        protocol_name=str(metadata.get("protocolName", path.stem)),
        api_level=api_level,
        maximum_supported_api_level=str(MAX_SUPPORTED_VERSION),
        robot_type=robot_type,
        labware_load_names=load_names,
        missing_labware=missing,
        shadow_substitutions=tuple(sorted(applied_shadow.items())),
        simulation_completed=simulation_completed,
        simulation_error=simulation_error,
        custom_labware_identities_verified=identities_verified,
        command_count=len(runlog),
        tip_pickups=tip_pickups,
        returned_tip_pickups=returns,
        discarded_tip_pickups=drop_commands - returns,
        gripper_moves=sum(" with gripper" in text for text in texts),
        aspirate_commands=sum(text.startswith("Aspirating ") for text in texts),
        dispense_commands=sum(text.startswith("Dispensing ") for text in texts),
        delay_seconds=delay_seconds,
        temperature_deactivations=sum(text.startswith("Deactivating Temperature Module") for text in texts),
    )


__all__ = ["ProtocolPreflightReport", "inspect_protocol"]
