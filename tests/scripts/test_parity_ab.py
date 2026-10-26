"""Unit tests for the native-vs-primitive parity script."""

from __future__ import annotations

import pytest

from scripts import parity_ab


class TestNativeProtocolSource:
    def test_generates_valid_python_with_parameters(self) -> None:
        source = parity_ab.native_protocol_source(
            volume=25.0,
            transfers=8,
            tip_pickup=(10.0, 20.0, 30.0),
            tip_drop=(40.0, 50.0, 60.0),
            source=(70.0, 80.0, 90.0),
            destination=(100.0, 110.0, 120.0),
        )
        assert 'metadata = {"apiLevel": "2.27"}' in source
        assert "for _ in range(8):" in source
        assert "pipette.aspirate(25.0)" in source
        assert "pipette.dispense(25.0)" in source
        assert "Point(70.0, 80.0, 90.0)" in source
        assert "Point(100.0, 110.0, 120.0)" in source
        assert "flex_1channel_1000" in source
        # The generated source must compile.
        compile(source, "<generated>", "exec")

    def test_transfer_count_is_parameterized(self) -> None:
        source = parity_ab.native_protocol_source(
            volume=50.0,
            transfers=3,
            tip_pickup=(1.0, 2.0, 3.0),
            tip_drop=(4.0, 5.0, 6.0),
            source=(7.0, 8.0, 9.0),
            destination=(10.0, 11.0, 12.0),
        )
        assert "for _ in range(3):" in source


class TestBuildReport:
    def test_identical_arms_pass(self) -> None:
        report = parity_ab.build_report(
            robot="flex-1",
            volume=25.0,
            transfers=8,
            native_sequence=["pickUpTip", "aspirate", "dispense"],
            primitive_sequence=["PickUpTip", "TransferWithVerifiedLiquidClass"],
            native_weight_g=0.19964,  # 200 uL at 0.9982 g/mL
            primitive_weight_g=0.19964,
            density_g_ml=0.9982,
            started=0.0,
        )
        assert report["expected_total_ul"] == pytest.approx(200.0)
        assert report["native"]["measured_ul"] == pytest.approx(200.0, rel=1e-3)
        assert report["primitive"]["measured_ul"] == pytest.approx(200.0, rel=1e-3)
        assert report["arm_difference_pct"] == pytest.approx(0.0)
        assert report["overall_pass"] is True

    def test_arm_offset_fails(self) -> None:
        report = parity_ab.build_report(
            robot="flex-1",
            volume=25.0,
            transfers=8,
            native_sequence=[],
            primitive_sequence=[],
            native_weight_g=0.19,  # 190.3 uL -> ~-4.8%
            primitive_weight_g=0.19964,
            density_g_ml=0.9982,
            started=0.0,
        )
        assert report["overall_pass"] is False
        assert report["native"]["error_pct"] < -2.0

    def test_command_counts_recorded(self) -> None:
        report = parity_ab.build_report(
            robot="flex-1",
            volume=25.0,
            transfers=8,
            native_sequence=["a", "b", "c"],
            primitive_sequence=["x"],
            native_weight_g=0.19964,
            primitive_weight_g=0.19964,
            density_g_ml=0.9982,
            started=0.0,
        )
        assert report["native"]["command_count"] == 3
        assert report["primitive"]["command_count"] == 1
