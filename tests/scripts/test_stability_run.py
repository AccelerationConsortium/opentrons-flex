"""Unit tests for the stability-run campaign script."""

from __future__ import annotations

import pytest

from scripts import stability_run


def _cycle(ok: bool, total: int, failed: int, retries: int, elapsed: float = 10.0) -> dict:
    return {
        "run": 1,
        "ok": ok,
        "elapsed_s": elapsed,
        "commands_total": total,
        "commands_failed": failed,
        "retries": retries,
        "failures": [],
    }


class TestBuildReport:
    def test_perfect_run(self) -> None:
        cycles = [
            _cycle(True, 4, 0, 0, 8.0),
            _cycle(True, 4, 0, 0, 12.0),
            _cycle(True, 4, 0, 0, 10.0),
        ]
        report = stability_run.build_report(
            robot="flex-1",
            runs=3,
            volume_ul=50.0,
            cycles=cycles,
            drift_events=[],
            drift_mm=0.5,
            started=0.0,
        )
        assert report["completion_rate"] == pytest.approx(1.0)
        assert report["command_failure_rate"] == pytest.approx(0.0)
        assert report["retries"] == 0
        assert report["mean_cycle_s"] == pytest.approx(10.0)
        assert report["min_cycle_s"] == pytest.approx(8.0)
        assert report["max_cycle_s"] == pytest.approx(12.0)
        assert report["overall_pass"] is True

    def test_failed_cycle_lowers_completion_rate(self) -> None:
        cycles = [
            _cycle(True, 4, 0, 0),
            _cycle(False, 6, 2, 1),
            _cycle(True, 4, 0, 0),
        ]
        report = stability_run.build_report(
            robot="flex-1",
            runs=3,
            volume_ul=50.0,
            cycles=cycles,
            drift_events=[],
            drift_mm=0.5,
            started=0.0,
        )
        assert report["completion_rate"] == pytest.approx(0.6667)
        assert report["command_failure_rate"] == pytest.approx(2 / 14, rel=1e-3)
        assert report["retries"] == 1
        assert report["overall_pass"] is False

    def test_drift_event_fails_campaign(self) -> None:
        cycles = [_cycle(True, 4, 0, 0)]
        report = stability_run.build_report(
            robot="flex-1",
            runs=1,
            volume_ul=50.0,
            cycles=cycles,
            drift_events=[{"run": 1, "drift_mm": 2.0}],
            drift_mm=0.5,
            started=0.0,
        )
        assert report["overall_pass"] is False
        assert len(report["drift_events"]) == 1

    def test_empty_cycles(self) -> None:
        report = stability_run.build_report(
            robot="flex-1",
            runs=0,
            volume_ul=50.0,
            cycles=[],
            drift_events=[],
            drift_mm=0.5,
            started=0.0,
        )
        assert report["completion_rate"] == pytest.approx(0.0)
        assert report["mean_cycle_s"] is None


class TestDistance:
    def test_zero(self) -> None:
        assert stability_run._distance((1.0, 2.0, 3.0), (1.0, 2.0, 3.0)) == pytest.approx(0.0)

    def test_known(self) -> None:
        assert stability_run._distance((0.0, 0.0, 0.0), (3.0, 4.0, 0.0)) == pytest.approx(5.0)
