"""Preserve hardware API mount identity when reporting installed pipettes."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from opentrons.types import Mount
from opentrons.hardware_control.types import OT3Mount
from unitelabs.opentrons_flex.features.pipette import PipetteFeature
from unitelabs.opentrons_flex.features._pipette_types import PipetteMount


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [Mount.LEFT, OT3Mount.LEFT])
async def test_pipette_scan_accepts_hardware_and_internal_mount_keys(key):
    controller = MagicMock()
    controller.cache_instruments = AsyncMock()
    controller.attached_instruments = {
        key: {
            "model": "p1000_single_v3.0",
            "name": "test pipette",
            "pipette_id": "fixture-left",
            "channels": 1,
            "min_volume": 5,
            "max_volume": 1000,
            "has_tip": False,
        }
    }
    feature = PipetteFeature(controller)
    result = await feature.get_attached_pipettes(status=MagicMock(), intermediate=MagicMock())
    left = next(item for item in result if item.mount is PipetteMount.LEFT)
    assert left.attached is True
    assert left.pipette_id == "fixture-left"
    assert left.channels == 1
    right = next(item for item in result if item.mount is PipetteMount.RIGHT)
    assert right.attached is False
