"""Cross-runtime contract shared by the Flex connector and workflow engine."""

from importlib.metadata import PackageNotFoundError, version

from .acceptance import (
    AcceptanceManifest,
    Coordinate,
    ModuleConfig,
    PipettingConfig,
    PlanContract,
    ThermocyclerStepConfig,
    WellGeometryConfig,
    validate_plan_contract,
)
from .wire_types import (
    LiquidPosition,
    Mount,
    PipetteMount,
    ThermocyclerProfileStep,
    TipLocation,
    TransferProfile,
    VerifiedLiquidClass,
    WellGeometry,
)

try:
    __version__ = version("unitelabs-flex-acceptance-contract")
except PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = [
    "AcceptanceManifest",
    "Coordinate",
    "LiquidPosition",
    "ModuleConfig",
    "Mount",
    "PipetteMount",
    "PipettingConfig",
    "PlanContract",
    "ThermocyclerProfileStep",
    "ThermocyclerStepConfig",
    "TipLocation",
    "TransferProfile",
    "VerifiedLiquidClass",
    "WellGeometry",
    "WellGeometryConfig",
    "__version__",
    "validate_plan_contract",
]
