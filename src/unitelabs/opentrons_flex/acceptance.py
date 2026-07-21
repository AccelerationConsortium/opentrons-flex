"""Strict client-side manifest for the Flex system acceptance workflow."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path


def _object(value: object, label: str, fields: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        msg = f"{label} must be a JSON object."
        raise ValueError(msg)
    missing = fields - set(value)
    unknown = set(value) - fields
    if missing:
        msg = f"{label} is missing required fields: {', '.join(sorted(missing))}."
        raise ValueError(msg)
    if unknown:
        msg = f"{label} contains unknown fields: {', '.join(sorted(unknown))}."
        raise ValueError(msg)
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"{label} must be a non-empty string."
        raise ValueError(msg)
    normalized = value.strip()
    if any(marker in normalized.upper() for marker in ("REPLACE", "YOUR_", "<ROBOT", "<MODULE")):
        msg = f"{label} still contains a deployment placeholder."
        raise ValueError(msg)
    return normalized


def _number(value: object, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        msg = f"{label} must be a finite number."
        raise ValueError(msg)
    number = float(value)
    if not minimum <= number <= maximum:
        msg = f"{label} must be between {minimum} and {maximum}."
        raise ValueError(msg)
    return number


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{label} must be an integer."
        raise ValueError(msg)
    if not minimum <= value <= maximum:
        msg = f"{label} must be between {minimum} and {maximum}."
        raise ValueError(msg)
    return value


@dataclass(frozen=True)
class Coordinate:
    """Absolute calibrated deck coordinate in millimetres."""

    x: float
    y: float
    z: float

    @classmethod
    def parse(cls, value: object, label: str) -> Coordinate:
        """Parse one strictly shaped coordinate object."""
        data = _object(value, label, {"x", "y", "z"})
        return cls(
            x=_number(data["x"], f"{label}.x", -1000.0, 1000.0),
            y=_number(data["y"], f"{label}.y", -1000.0, 1000.0),
            z=_number(data["z"], f"{label}.z", -1000.0, 1000.0),
        )


@dataclass(frozen=True)
class WellGeometryConfig:
    """Calibrated well centre and vertical geometry in millimetres."""

    x: float
    y: float
    bottom_z: float
    top_z: float
    length: float
    width: float

    @classmethod
    def parse(cls, value: object, label: str) -> WellGeometryConfig:
        """Parse and validate one well geometry object."""
        fields = {"x", "y", "bottom_z", "top_z", "length", "width"}
        data = _object(value, label, fields)
        parsed = cls(
            x=_number(data["x"], f"{label}.x", -1000.0, 1000.0),
            y=_number(data["y"], f"{label}.y", -1000.0, 1000.0),
            bottom_z=_number(data["bottom_z"], f"{label}.bottom_z", -1000.0, 1000.0),
            top_z=_number(data["top_z"], f"{label}.top_z", -1000.0, 1000.0),
            length=_number(data["length"], f"{label}.length", 0.1, 200.0),
            width=_number(data["width"], f"{label}.width", 0.1, 200.0),
        )
        if parsed.bottom_z >= parsed.top_z:
            msg = f"{label}.bottom_z must be below top_z."
            raise ValueError(msg)
        return parsed


@dataclass(frozen=True)
class PipettingConfig:
    """Prepared safe-liquid transfer geometry and operating values."""

    mount: str
    tip_length: float
    tiprack_diameter: float
    tip_pickup: Coordinate
    tip_drop: Coordinate
    source: Coordinate
    source_retract: Coordinate
    destination: Coordinate
    destination_retract: Coordinate
    source_well: WellGeometryConfig
    destination_well: WellGeometryConfig
    transfer_volume: float
    prepared_source_volume: float
    probe_maximum_distance: float
    mix_volume: float
    mix_cycles: int
    tiprack_uri: str

    @classmethod
    def parse(cls, value: object) -> PipettingConfig:
        """Parse the complete prepared liquid-handling configuration."""
        fields = {
            "mount",
            "tip_length",
            "tiprack_diameter",
            "tip_pickup",
            "tip_drop",
            "source",
            "source_retract",
            "destination",
            "destination_retract",
            "source_well",
            "destination_well",
            "transfer_volume",
            "prepared_source_volume",
            "probe_maximum_distance",
            "mix_volume",
            "mix_cycles",
            "tiprack_uri",
        }
        data = _object(value, "pipetting", fields)
        mount = _text(data["mount"], "pipetting.mount").upper()
        if mount not in {"LEFT", "RIGHT"}:
            msg = "pipetting.mount must be LEFT or RIGHT."
            raise ValueError(msg)
        transfer_volume = _number(data["transfer_volume"], "pipetting.transfer_volume", 1.0, 1000.0)
        mix_volume = _number(data["mix_volume"], "pipetting.mix_volume", 1.0, 1000.0)
        prepared_source_volume = _number(
            data["prepared_source_volume"], "pipetting.prepared_source_volume", 1.0, 20000.0
        )
        minimum_required = transfer_volume * 2.0 + mix_volume
        if prepared_source_volume < minimum_required:
            msg = (
                "pipetting.prepared_source_volume must cover two transfers plus one mix "
                f"({minimum_required:g} microlitres minimum)."
            )
            raise ValueError(msg)
        parsed = cls(
            mount=mount,
            tip_length=_number(data["tip_length"], "pipetting.tip_length", 1.0, 100.0),
            tiprack_diameter=_number(data["tiprack_diameter"], "pipetting.tiprack_diameter", 0.1, 20.0),
            tip_pickup=Coordinate.parse(data["tip_pickup"], "pipetting.tip_pickup"),
            tip_drop=Coordinate.parse(data["tip_drop"], "pipetting.tip_drop"),
            source=Coordinate.parse(data["source"], "pipetting.source"),
            source_retract=Coordinate.parse(data["source_retract"], "pipetting.source_retract"),
            destination=Coordinate.parse(data["destination"], "pipetting.destination"),
            destination_retract=Coordinate.parse(data["destination_retract"], "pipetting.destination_retract"),
            source_well=WellGeometryConfig.parse(data["source_well"], "pipetting.source_well"),
            destination_well=WellGeometryConfig.parse(data["destination_well"], "pipetting.destination_well"),
            transfer_volume=transfer_volume,
            prepared_source_volume=prepared_source_volume,
            probe_maximum_distance=_number(
                data["probe_maximum_distance"], "pipetting.probe_maximum_distance", 1.0, 50.0
            ),
            mix_volume=mix_volume,
            mix_cycles=_integer(data["mix_cycles"], "pipetting.mix_cycles", 1, 20),
            tiprack_uri=_text(data["tiprack_uri"], "pipetting.tiprack_uri"),
        )
        for label, position, retract in (
            ("source", parsed.source, parsed.source_retract),
            ("destination", parsed.destination, parsed.destination_retract),
        ):
            if (position.x, position.y) != (retract.x, retract.y) or retract.z <= position.z:
                msg = f"pipetting.{label}_retract must be vertically above the matching {label} position."
                raise ValueError(msg)
        for label, position, well in (
            ("source", parsed.source, parsed.source_well),
            ("destination", parsed.destination, parsed.destination_well),
        ):
            if (position.x, position.y) != (well.x, well.y) or not well.bottom_z <= position.z <= well.top_z:
                msg = f"pipetting.{label} must be inside the matching well geometry."
                raise ValueError(msg)
        if parsed.probe_maximum_distance < parsed.source_retract.z - parsed.source.z:
            msg = "pipetting.probe_maximum_distance must reach from source_retract to source."
            raise ValueError(msg)
        return parsed


@dataclass(frozen=True)
class ThermocyclerStepConfig:
    """One guarded Thermocycler profile step."""

    temperature: float
    hold_time: float
    ramp_rate: float

    @classmethod
    def parse(cls, value: object, index: int) -> ThermocyclerStepConfig:
        """Parse one constrained thermal profile step."""
        label = f"thermocycler.profile[{index}]"
        data = _object(value, label, {"temperature", "hold_time", "ramp_rate"})
        return cls(
            temperature=_number(data["temperature"], f"{label}.temperature", 4.0, 99.0),
            hold_time=_number(data["hold_time"], f"{label}.hold_time", 0.0, 3600.0),
            ramp_rate=_number(data["ramp_rate"], f"{label}.ramp_rate", 0.0, 10.0),
        )


@dataclass(frozen=True)
class ModuleConfig:
    """Safe operating values and required module serial numbers."""

    heater_shaker_serial: str
    heater_shaker_temperature: float
    heater_shaker_speed: int
    thermocycler_serial: str
    thermocycler_lid_temperature: float
    thermocycler_volume: float
    thermocycler_profile: tuple[ThermocyclerStepConfig, ...]
    temperature_module_serial: str
    temperature_module_target: float
    plate_reader_serial: str
    plate_reader_wavelengths: tuple[int, ...]
    plate_reader_reference_wavelength: int
    stacker_serial: str
    stacker_labware_height: float

    @classmethod
    def parse(cls, value: object) -> ModuleConfig:
        """Parse accessory identities and guarded operating values."""
        fields = {"heater_shaker", "thermocycler", "temperature_module", "plate_reader", "stacker"}
        data = _object(value, "modules", fields)
        heater = _object(data["heater_shaker"], "modules.heater_shaker", {"serial", "temperature", "speed"})
        thermocycler = _object(
            data["thermocycler"],
            "modules.thermocycler",
            {"serial", "lid_temperature", "volume", "profile"},
        )
        temperature = _object(data["temperature_module"], "modules.temperature_module", {"serial", "target"})
        reader = _object(
            data["plate_reader"],
            "modules.plate_reader",
            {"serial", "wavelengths", "reference_wavelength"},
        )
        stacker = _object(data["stacker"], "modules.stacker", {"serial", "labware_height"})
        profile = thermocycler["profile"]
        wavelengths = reader["wavelengths"]
        if not isinstance(profile, list) or not 1 <= len(profile) <= 20:
            msg = "modules.thermocycler.profile must contain between 1 and 20 steps."
            raise ValueError(msg)
        if not isinstance(wavelengths, list) or not 2 <= len(wavelengths) <= 6:
            msg = "modules.plate_reader.wavelengths must contain between 2 and 6 wavelengths."
            raise ValueError(msg)
        parsed_wavelengths = tuple(
            _integer(item, f"modules.plate_reader.wavelengths[{index}]", 350, 1000)
            for index, item in enumerate(wavelengths)
        )
        if len(set(parsed_wavelengths)) != len(parsed_wavelengths):
            msg = "modules.plate_reader.wavelengths must contain unique wavelengths."
            raise ValueError(msg)
        reference = _integer(reader["reference_wavelength"], "modules.plate_reader.reference_wavelength", 350, 1000)
        if reference not in parsed_wavelengths:
            msg = "modules.plate_reader.reference_wavelength must also appear in wavelengths."
            raise ValueError(msg)
        if all(wavelength == reference for wavelength in parsed_wavelengths):
            msg = "modules.plate_reader.wavelengths must include a sample wavelength distinct from the reference."
            raise ValueError(msg)
        parsed_profile = tuple(ThermocyclerStepConfig.parse(item, index) for index, item in enumerate(profile))
        if sum(step.hold_time for step in parsed_profile) > 3600.0:
            msg = "modules.thermocycler.profile total hold time cannot exceed 3600 seconds."
            raise ValueError(msg)
        return cls(
            heater_shaker_serial=_text(heater["serial"], "modules.heater_shaker.serial"),
            heater_shaker_temperature=_number(heater["temperature"], "modules.heater_shaker.temperature", 37.0, 95.0),
            heater_shaker_speed=_integer(heater["speed"], "modules.heater_shaker.speed", 200, 3000),
            thermocycler_serial=_text(thermocycler["serial"], "modules.thermocycler.serial"),
            thermocycler_lid_temperature=_number(
                thermocycler["lid_temperature"], "modules.thermocycler.lid_temperature", 37.0, 110.0
            ),
            thermocycler_volume=_number(thermocycler["volume"], "modules.thermocycler.volume", 1.0, 200.0),
            thermocycler_profile=parsed_profile,
            temperature_module_serial=_text(temperature["serial"], "modules.temperature_module.serial"),
            temperature_module_target=_number(temperature["target"], "modules.temperature_module.target", 4.0, 95.0),
            plate_reader_serial=_text(reader["serial"], "modules.plate_reader.serial"),
            plate_reader_wavelengths=parsed_wavelengths,
            plate_reader_reference_wavelength=reference,
            stacker_serial=_text(stacker["serial"], "modules.stacker.serial"),
            stacker_labware_height=_number(stacker["labware_height"], "modules.stacker.labware_height", 4.0, 102.5),
        )


_PLAN_FIELDS = {
    "process_deck_to_thermocycler",
    "thermocycler_to_process_deck",
    "process_deck_to_heater_shaker",
    "heater_shaker_to_magnetic_block",
    "magnetic_block_to_process_deck",
    "assay_deck_to_temperature_module",
    "temperature_module_to_assay_deck",
    "reader_lid_open",
    "reader_lid_close",
    "assay_deck_to_reader",
    "reader_to_assay_deck",
}


@dataclass(frozen=True)
class AcceptanceManifest:
    """Complete, fail-closed configuration for a physical acceptance campaign."""

    schema_version: int
    service_name: str
    expected_robot_host: str
    pipetting: PipettingConfig
    modules: ModuleConfig
    plans: Mapping[str, str]

    @classmethod
    def parse(cls, value: object) -> AcceptanceManifest:
        """Parse and reject any incomplete or ambiguous campaign manifest."""
        fields = {"schema_version", "service_name", "expected_robot_host", "pipetting", "modules", "plans"}
        data = _object(value, "acceptance manifest", fields)
        version = _integer(data["schema_version"], "schema_version", 1, 1)
        plans = _object(data["plans"], "plans", _PLAN_FIELDS)
        parsed_plans = {name: _text(plans[name], f"plans.{name}") for name in sorted(_PLAN_FIELDS)}
        if len(set(parsed_plans.values())) != len(parsed_plans):
            msg = "Every acceptance labware plan identifier must be unique."
            raise ValueError(msg)
        return cls(
            schema_version=version,
            service_name=_text(data["service_name"], "service_name"),
            expected_robot_host=_text(data["expected_robot_host"], "expected_robot_host"),
            pipetting=PipettingConfig.parse(data["pipetting"]),
            modules=ModuleConfig.parse(data["modules"]),
            plans=parsed_plans,
        )

    @classmethod
    def load(cls, path: str | Path) -> AcceptanceManifest:
        """Load and validate a manifest before any connector call is attempted."""
        manifest_path = Path(path).expanduser()
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            msg = f"Cannot load acceptance manifest {manifest_path}: {exc}"
            raise ValueError(msg) from exc
        return cls.parse(value)

    def commissioning_digest(self) -> str:
        """Return a stable SHA-256 fingerprint for an operator-approved manifest."""
        payload = asdict(self)
        payload["plans"] = dict(self.plans)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PlanContract:
    """Non-geometric metadata used to validate an allowlisted movement graph."""

    plan_identifier: str
    labware_identifier: str
    source_location: str
    destination_location: str
    is_lid: bool


def validate_plan_contract(
    manifest: AcceptanceManifest,
    plans: tuple[PlanContract, ...],
    *,
    deck_valid: bool,
    occupancy: Mapping[str, str],
) -> None:
    """Fail closed unless plans and durable occupancy form the expected campaign graph."""
    by_identifier = {plan.plan_identifier: plan for plan in plans}
    missing = set(manifest.plans.values()) - set(by_identifier)
    if missing:
        msg = f"Server is missing acceptance movement plans: {sorted(missing)}."
        raise ValueError(msg)
    named = {name: by_identifier[identifier] for name, identifier in manifest.plans.items()}
    lid_names = {"reader_lid_open", "reader_lid_close"}
    for name, plan in named.items():
        if plan.is_lid is not (name in lid_names):
            msg = f"Plan {plan.plan_identifier!r} has the wrong lid classification."
            raise ValueError(msg)

    def reversible(outbound: str, inbound: str) -> None:
        first = named[outbound]
        second = named[inbound]
        if (
            first.labware_identifier != second.labware_identifier
            or first.source_location != second.destination_location
            or first.destination_location != second.source_location
        ):
            msg = f"Acceptance plans {outbound!r} and {inbound!r} are not a reversible pair."
            raise ValueError(msg)

    reversible("process_deck_to_thermocycler", "thermocycler_to_process_deck")
    reversible("assay_deck_to_temperature_module", "temperature_module_to_assay_deck")
    reversible("reader_lid_open", "reader_lid_close")
    reversible("assay_deck_to_reader", "reader_to_assay_deck")

    process_chain = (
        named["thermocycler_to_process_deck"],
        named["process_deck_to_heater_shaker"],
        named["heater_shaker_to_magnetic_block"],
        named["magnetic_block_to_process_deck"],
    )
    if len({plan.labware_identifier for plan in process_chain}) != 1:
        msg = "Thermocycler, Heater-Shaker, and Magnetic Block plans must use one process plate."
        raise ValueError(msg)
    process_locations = (
        (process_chain[0].destination_location, process_chain[1].source_location),
        (process_chain[1].destination_location, process_chain[2].source_location),
        (process_chain[2].destination_location, process_chain[3].source_location),
        (process_chain[3].destination_location, process_chain[1].source_location),
    )
    if any(left != right for left, right in process_locations):
        msg = "Process-plate movement plans do not form a closed location chain."
        raise ValueError(msg)

    temperature_return = named["temperature_module_to_assay_deck"]
    reader_outbound = named["assay_deck_to_reader"]
    if (
        temperature_return.labware_identifier != reader_outbound.labware_identifier
        or temperature_return.destination_location != reader_outbound.source_location
    ):
        msg = "Temperature Module and Plate Reader plans must share one assay plate and deck location."
        raise ValueError(msg)
    if named["reader_lid_open"].labware_identifier == reader_outbound.labware_identifier:
        msg = "The Plate Reader lid and assay plate must have distinct labware identifiers."
        raise ValueError(msg)

    if not deck_valid:
        msg = "Connector deck ledger is invalid. Reconcile physical locations before acceptance."
        raise ValueError(msg)
    for name in ("process_deck_to_thermocycler", "assay_deck_to_temperature_module", "reader_lid_open"):
        plan = named[name]
        if occupancy.get(plan.source_location) != plan.labware_identifier:
            msg = (
                f"{plan.labware_identifier!r} must start at {plan.source_location!r}; "
                f"deck ledger reports {occupancy.get(plan.source_location)!r}."
            )
            raise ValueError(msg)


__all__ = [
    "AcceptanceManifest",
    "Coordinate",
    "ModuleConfig",
    "PipettingConfig",
    "PlanContract",
    "ThermocyclerStepConfig",
    "WellGeometryConfig",
    "validate_plan_contract",
]
