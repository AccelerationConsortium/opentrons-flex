"""Unit tests for the gravimetric volume-accuracy campaign script."""

from __future__ import annotations

import math

import pytest

from scripts import volume_accuracy


class TestParseVolumes:
    def test_parses_comma_separated(self) -> None:
        assert volume_accuracy.parse_volumes("10,25,50") == [10.0, 25.0, 50.0]

    def test_tolerates_whitespace(self) -> None:
        assert volume_accuracy.parse_volumes(" 10 , 25 ") == [10.0, 25.0]

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="at least one volume"):
            volume_accuracy.parse_volumes("")

    def test_rejects_non_numeric(self) -> None:
        with pytest.raises(ValueError, match="Invalid volume"):
            volume_accuracy.parse_volumes("10,abc")

    def test_rejects_non_positive(self) -> None:
        with pytest.raises(ValueError, match="greater than zero"):
            volume_accuracy.parse_volumes("10,-5")

    def test_rejects_non_finite(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            volume_accuracy.parse_volumes("10,nan")


class TestWaterVolumeFromWeight:
    def test_standard_density(self) -> None:
        # 25.0 mg of water at 0.9982 g/mL == 25.045 uL
        assert volume_accuracy.water_volume_from_weight(0.025, 0.9982) == pytest.approx(25.0451, rel=1e-4)

    def test_zero_weight(self) -> None:
        assert volume_accuracy.water_volume_from_weight(0.0, 0.9982) == pytest.approx(0.0)

    def test_rejects_bad_weight(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            volume_accuracy.water_volume_from_weight(math.inf, 0.9982)

    def test_rejects_bad_density(self) -> None:
        with pytest.raises(ValueError, match="Density"):
            volume_accuracy.water_volume_from_weight(0.1, 0.0)


class TestSummarize:
    def test_perfect_measurements(self) -> None:
        measurements = [
            {"target_ul": 25.0, "measured_ul": 25.0, "error_ul": 0.0, "error_pct": 0.0},
            {"target_ul": 25.0, "measured_ul": 25.0, "error_ul": 0.0, "error_pct": 0.0},
            {"target_ul": 25.0, "measured_ul": 25.0, "error_ul": 0.0, "error_pct": 0.0},
        ]
        summary = volume_accuracy.summarize_measurements(measurements)
        assert summary["n"] == 3
        assert summary["mean_ul"] == pytest.approx(25.0)
        assert summary["std_ul"] == pytest.approx(0.0)
        assert summary["cv_pct"] == pytest.approx(0.0)
        assert summary["max_error_pct"] == pytest.approx(0.0)

    def test_with_offset_and_spread(self) -> None:
        measurements = [
            {"target_ul": 100.0, "measured_ul": 99.0, "error_ul": -1.0, "error_pct": -1.0},
            {"target_ul": 100.0, "measured_ul": 101.0, "error_ul": 1.0, "error_pct": 1.0},
            {"target_ul": 100.0, "measured_ul": 100.0, "error_ul": 0.0, "error_pct": 0.0},
        ]
        summary = volume_accuracy.summarize_measurements(measurements)
        assert summary["mean_ul"] == pytest.approx(100.0)
        assert summary["cv_pct"] == pytest.approx(1.0)
        assert summary["max_error_pct"] == pytest.approx(1.0)
        assert summary["min_ul"] == pytest.approx(99.0)
        assert summary["max_ul"] == pytest.approx(101.0)

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            volume_accuracy.summarize_measurements([])


class TestVerdict:
    def test_pass_within_limits(self) -> None:
        summary = {"max_error_pct": 1.0, "cv_pct": 0.5}
        result = volume_accuracy.verdict(summary, tolerance_pct=2.0, repeatability_pct=1.0)
        assert result["pass"] is True

    def test_fail_on_accuracy(self) -> None:
        summary = {"max_error_pct": 3.0, "cv_pct": 0.5}
        result = volume_accuracy.verdict(summary, tolerance_pct=2.0, repeatability_pct=1.0)
        assert result["pass"] is False
        assert result["accuracy_pass"] is False

    def test_fail_on_repeatability(self) -> None:
        summary = {"max_error_pct": 1.0, "cv_pct": 1.5}
        result = volume_accuracy.verdict(summary, tolerance_pct=2.0, repeatability_pct=1.0)
        assert result["pass"] is False
        assert result["repeatability_pass"] is False


class TestBuildReport:
    def test_overall_pass_folds_band_verdicts(self) -> None:
        measurements = [
            {"target_ul": 25.0, "measured_ul": 25.0, "error_ul": 0.0, "error_pct": 0.0},
        ]
        summaries = [
            {"target_ul": 25.0, "n": 1, "mean_ul": 25.0, "std_ul": 0.0, "cv_pct": 0.0, "max_error_pct": 0.0}
        ]
        report = volume_accuracy.build_report(
            robot="flex-1",
            pipette={"mount": "LEFT", "channels": 1},
            density_g_ml=0.9982,
            measurements=measurements,
            summaries=summaries,
            command_feedback=[{"command": "PickUpTip", "ok": True}],
            tolerance_pct=2.0,
            repeatability_pct=1.0,
            start_time=0.0,
            mode="verified-water",
        )
        assert report["overall_pass"] is True
        assert report["summaries"][0]["pass"] is True
        assert report["schema"] == "volume-accuracy/v1"

    def test_overall_fail_when_any_band_fails(self) -> None:
        summaries = [
            {"target_ul": 25.0, "n": 1, "mean_ul": 25.0, "std_ul": 0.0, "cv_pct": 0.0, "max_error_pct": 5.0}
        ]
        report = volume_accuracy.build_report(
            robot="flex-1",
            pipette={"mount": "LEFT", "channels": 1},
            density_g_ml=0.9982,
            measurements=[],
            summaries=summaries,
            command_feedback=[],
            tolerance_pct=2.0,
            repeatability_pct=1.0,
            start_time=0.0,
            mode="verified-water",
        )
        assert report["overall_pass"] is False


class TestCsvRendering:
    def test_measurements_csv_header_and_row(self) -> None:
        csv_text = volume_accuracy.render_measurements_csv(
            [{"target_ul": 25.0, "replicate": 1, "measured_ul": 24.8}]
        )
        lines = csv_text.strip().splitlines()
        assert lines[0] == "target_ul,replicate,measured_ul,error_ul,error_pct"
        assert lines[1].startswith("25.0,1,24.8000,")

    def test_summary_csv_contains_verdicts(self) -> None:
        csv_text = volume_accuracy.render_summary_csv(
            [
                {
                    "target_ul": 25.0,
                    "n": 3,
                    "mean_ul": 24.9,
                    "std_ul": 0.1,
                    "cv_pct": 0.4,
                    "mean_error_pct": -0.4,
                    "max_error_pct": 0.8,
                    "accuracy_pass": True,
                    "repeatability_pass": True,
                    "pass": True,
                }
            ]
        )
        assert "target_ul,n,mean_ul,std_ul,cv_pct,mean_error_pct,max_error_pct" in csv_text
        assert "25.0,3,24.9,0.1,0.4,-0.4,0.8,True,True,True" in csv_text
