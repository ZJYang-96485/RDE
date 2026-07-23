from __future__ import annotations

import csv
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app
from analysis.ca_charge import (
    CaChargeAnalysisError,
    LiveChargeDecorator,
    load_charge_series,
    run_ca_charge_analysis,
)
from analysis.integration import StreamingTrapezoidAccumulator, cumulative_trapezoid
from gamry_worker.live_writer import initialize_live_stream, read_live_points, read_live_status
from gamry_worker.live_adapters import LiveCurveEmitter, normalize_ca_acq_rows
from gamry_worker.mock_gamry import LiveEmitter, run_ca, run_ca_staircase
from workflow.data_manager import (
    export_run_dta_to_csv,
    load_manifest,
    register_trial_analysis_result,
    save_manifest,
    write_run_summary,
)
from workflow.history_artifacts import list_analysis_groups, resolve_registered_artifact
from workflow.protocol_loader import ProtocolError, validate_protocol_payload
from workflow.recipe_runner import (
    run_protocol_for_sample,
    run_requested_post_acquisition_analyses,
)
from workflow import state


def write_mock_ca(path: Path, rows: list[tuple[object, object, object]]) -> None:
    lines = [
        "MOCK_GAMRY_DATA",
        "TECHNIQUE\tca",
        "time_s\tpotential_v\tcurrent_a",
    ]
    lines.extend("\t".join(str(value) for value in row) for row in rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TrapezoidCoreTest(unittest.TestCase):
    def assertCharge(
        self,
        times: list[float],
        currents: list[float],
        expected: float,
    ) -> None:
        result = cumulative_trapezoid(times, currents)
        self.assertAlmostEqual(result.final_integral, expected, places=12)
        self.assertTrue(math.isfinite(result.final_integral))

    def test_zero_positive_negative_and_linear_currents(self) -> None:
        self.assertCharge([0, 1, 2], [0, 0, 0], 0)
        self.assertCharge([0, 1, 2], [2, 2, 2], 4)
        self.assertCharge([0, 1, 2], [-2, -2, -2], -4)
        self.assertCharge([0, 1, 2], [0, 1, 2], 2)

    def test_nonuniform_sampling_uses_actual_interval_widths(self) -> None:
        # Integral of I=t over [0, 3] is exactly 4.5 C for trapezoids.
        self.assertCharge([0, 0.25, 1.5, 3], [0, 0.25, 1.5, 3], 4.5)

    def test_single_point_returns_finite_zero(self) -> None:
        empty = cumulative_trapezoid([], [])
        self.assertEqual(empty.final_integral, 0)
        self.assertEqual(empty.source_point_count, 0)

        result = cumulative_trapezoid([4.2], [-3.0])
        self.assertEqual(result.final_integral, 0)
        self.assertEqual(result.integrated_interval_count, 0)
        self.assertEqual(result.skipped_interval_count, 0)

    def test_duplicate_and_backward_timestamps_are_skipped_without_reset(self) -> None:
        duplicate = cumulative_trapezoid([0, 1, 1, 2], [1, 1, 3, 3])
        self.assertAlmostEqual(duplicate.final_integral, 4)
        self.assertEqual(duplicate.integrated_interval_count, 2)
        self.assertEqual(duplicate.skipped_interval_count, 1)
        self.assertTrue(duplicate.time_monotonic)
        self.assertTrue(any("duplicate" in warning for warning in duplicate.warnings))

        backward = cumulative_trapezoid([0, 2, 1, 3], [1, 1, 1, 1])
        self.assertAlmostEqual(backward.final_integral, 4)
        self.assertEqual(backward.skipped_interval_count, 1)
        self.assertFalse(backward.time_monotonic)
        self.assertTrue(any("nonmonotonic" in warning for warning in backward.warnings))

    def test_nonfinite_endpoints_never_produce_nan(self) -> None:
        result = cumulative_trapezoid(
            [0, 1, 2, 3, 4],
            [1, float("nan"), 2, float("inf"), 3],
        )
        self.assertEqual(result.final_integral, 0)
        self.assertTrue(math.isfinite(result.final_integral))
        self.assertEqual(result.skipped_interval_count, 4)
        self.assertTrue(result.warnings)

    def test_live_replay_is_idempotent(self) -> None:
        live = StreamingTrapezoidAccumulator(deduplicate=True)
        live.add_point(0, 1)
        live.add_point(1, 1)
        charge_before_replay = live.cumulative_integral
        live.add_point(0, 1)
        live.add_point(1, 1)
        self.assertEqual(live.cumulative_integral, charge_before_replay)
        self.assertTrue(any("replay" in warning for warning in live.warnings))

    def test_streaming_and_batch_match_for_clean_data(self) -> None:
        times = [0, 0.4, 1.1, 2.0]
        currents = [-2e-6, -1e-6, 3e-6, 4e-6]
        live = LiveChargeDecorator()
        decorated = [
            live({"t_s": time_s, "i_a": current_a, "e_v": -0.4})
            for time_s, current_a in zip(times, currents)
        ]
        batch = cumulative_trapezoid(times, currents)
        self.assertAlmostEqual(decorated[-1]["q_live_c"], batch.final_integral, places=15)

    def test_new_streaming_trial_starts_at_zero(self) -> None:
        first_trial = LiveChargeDecorator()
        first_trial({"t_s": 0, "i_a": 2, "e_v": 0})
        first_trial({"t_s": 1, "i_a": 2, "e_v": 0})
        self.assertEqual(first_trial.status_fields()["charge_live_c"], 2)
        second_trial = LiveChargeDecorator()
        first_point = second_trial({"t_s": 0, "i_a": 10, "e_v": 0})
        self.assertEqual(first_point["q_live_c"], 0)

    def test_live_transform_error_is_nonfatal_to_emitter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            live_dir = Path(tmpdir) / "live"
            initialize_live_stream(live_dir, run_id="run", technique="ca")

            def fail_transform(point):
                raise RuntimeError("analysis-only failure")

            emitter = LiveCurveEmitter(
                live_dir,
                normalize_ca_acq_rows,
                point_transform=fail_transform,
            )
            emitted = emitter.emit_point({"t_s": 1, "e_v": 0, "i_a": 1})
            self.assertFalse(emitted)
            self.assertTrue(
                any(
                    "analysis-only failure" in error
                    for error in emitter.result_fields()["live_stream_errors"]
                )
            )
            self.assertEqual(read_live_points(live_dir), [])

    def test_live_ring_buffer_reset_does_not_duplicate_charge(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            live_dir = Path(tmpdir) / "live"
            initialize_live_stream(live_dir, run_id="run", technique="ca")
            decorator = LiveChargeDecorator()
            emitter = LiveCurveEmitter(
                live_dir,
                normalize_ca_acq_rows,
                point_transform=decorator,
            )
            first = {"time": 0, "vf": 0, "im": 1}
            second = {"time": 1, "vf": 0, "im": 1}
            third = {"time": 2, "vf": 0, "im": 1}
            emitter.emit_new([first, second])
            emitter.emit_new([second])
            emitter.emit_new([second, third])
            points = read_live_points(live_dir)
            self.assertEqual([point["q_live_c"] for point in points], [0, 1, 2])


class CaChargeArtifactTest(unittest.TestCase):
    def test_final_analysis_recomputes_from_dta_and_writes_machine_readable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw = Path(tmpdir) / "005_01_CA.DTA"
            write_mock_ca(raw, [(0, -0.4, 1), (1, -0.4, 2), (3, -0.4, 0)])
            original_dta = raw.read_bytes()

            result = run_ca_charge_analysis(raw)
            self.assertEqual(raw.read_bytes(), original_dta)
            self.assertAlmostEqual(
                result["summary"]["result"]["final_signed_charge_c"],
                3.5,
            )
            self.assertEqual(result["summary"]["source"]["label"], "Recomputed from DTA")
            self.assertEqual(result["summary"]["analysis_version"], "ca-charge-v1")
            self.assertEqual(result["summary"]["result"]["integrated_intervals"], 2)
            self.assertEqual(result["summary"]["result"]["skipped_intervals"], 0)

            series_path = Path(result["artifacts"]["series_csv"])
            summary_path = Path(result["artifacts"]["summary_json"])
            with series_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 3)
            self.assertEqual(
                list(rows[0]),
                ["time_s", "potential_v", "current_a", "cumulative_charge_c"],
            )
            summary_text = summary_path.read_text(encoding="utf-8")
            self.assertNotIn("NaN", summary_text)
            self.assertNotIn("Infinity", summary_text)
            self.assertEqual(json.loads(summary_text)["analysis_status"], "complete")

    def test_malformed_dta_fails_analysis_without_fabricated_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw = Path(tmpdir) / "bad.DTA"
            raw.write_text("time_s\tpotential_v\n0\t-0.4\n", encoding="utf-8")
            with self.assertRaises(CaChargeAnalysisError):
                run_ca_charge_analysis(raw)
            self.assertFalse(raw.with_name("bad_charge_analysis.json").exists())

    def test_history_registration_and_plot_adapter_are_extensible(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            raw = run_dir / "01_Sample" / "CA.DTA"
            write_mock_ca(raw, [(0, -0.2, 0), (1, -0.2, 2e-6)])
            save_manifest(
                run_dir,
                {
                    "run_id": "run",
                    "outputs": [{"outputs": [str(raw)], "technique": "ca"}],
                    "trials": [],
                    "analysis_results": [],
                },
            )
            analysis = run_ca_charge_analysis(raw)
            registered = register_trial_analysis_result(
                run_dir,
                raw_dta=raw,
                analysis_type=analysis["analysis_type"],
                analysis_version=analysis["analysis_version"],
                label="CA Cumulative Charge",
                technique="ca",
                status="complete",
                analysis_artifacts=analysis["artifacts"],
                summary=analysis["summary"],
            )
            export_run_dta_to_csv(run_dir)
            write_run_summary(run_dir)
            groups = list_analysis_groups(run_dir)
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["analysis_type"], "cumulative_charge")
            self.assertEqual(groups[0]["analysis_status"], "complete")
            self.assertEqual(len(groups[0]["summary_items"]), 9)
            self.assertEqual(
                next(
                    artifact
                    for artifact in groups[0]["artifacts"]
                    if artifact["kind"] == "series_csv"
                )["plot_kind"],
                "xy",
            )
            self.assertTrue(
                any(
                    artifact["kind"] == "raw_csv"
                    for artifact in groups[0]["artifacts"]
                )
            )
            series_path = resolve_registered_artifact(
                run_dir,
                registered["analysis_artifacts"]["series_csv"],
            )
            plot = load_charge_series(series_path)
            self.assertEqual(plot["x_label"], "Time (s)")
            self.assertIn(plot["charge_unit"], {"C", "mC", "µC"})
            self.assertEqual(plot["original_point_count"], 2)
            summary = json.loads(
                (run_dir / "run_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                summary["analysis_results"][0]["analysis_type"],
                "cumulative_charge",
            )

    def test_analysis_failure_is_recorded_but_does_not_raise_from_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            raw = run_dir / "01_Sample" / "CA.DTA"
            raw.parent.mkdir(parents=True)
            raw.write_text("time_s\tpotential_v\n0\t-0.4\n", encoding="utf-8")
            save_manifest(
                run_dir,
                {
                    "run_id": "run",
                    "outputs": [{"outputs": [str(raw)], "technique": "ca"}],
                    "trials": [],
                    "analysis_results": [],
                },
            )
            records = run_requested_post_acquisition_analyses(
                run_dir=run_dir,
                step={
                    "technique": "ca",
                    "analysis": {
                        "cumulative_charge": {
                            "enabled": True,
                            "method": "trapezoidal",
                        }
                    },
                },
                outputs=[str(raw)],
            )
            self.assertEqual(records[0]["analysis_status"], "failed")
            self.assertIn("missing", records[0]["error"])
            manifest = load_manifest(run_dir)
            self.assertEqual(manifest["analysis_results"][0]["analysis_status"], "failed")
            groups = list_analysis_groups(run_dir)
            self.assertEqual(groups[0]["analysis_status"], "failed")
            self.assertEqual(groups[0]["artifacts"], [])

    def test_protocol_runner_invokes_final_analysis_after_completed_dta(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            sample_dir = run_dir / "01_Sample"
            sample_dir.mkdir(parents=True)
            save_manifest(
                run_dir,
                {
                    "run_id": "run",
                    "outputs": [],
                    "trials": [],
                    "analysis_results": [],
                },
            )
            protocol = validate_protocol_payload(
                {
                    "protocol_name": "ca_charge",
                    "steps": [
                        {
                            "name": "CA charge",
                            "technique": "ca",
                            "output": "CA.DTA",
                            "voltage_v": -0.2,
                            "duration_s": 2,
                            "sample_period_s": 1,
                            "analysis": {
                                "cumulative_charge": {
                                    "enabled": True,
                                    "method": "trapezoidal",
                                }
                            },
                        }
                    ],
                }
            )

            def fake_gamry_step(*, outputs, **kwargs):
                write_mock_ca(
                    Path(outputs[0]),
                    [(0, -0.2, 1e-6), (1, -0.2, 1e-6), (2, -0.2, 1e-6)],
                )
                return {
                    "ok": True,
                    "trial_metadata": {"trial_status": "completed"},
                }

            with patch(
                "workflow.recipe_runner.run_gamry_step",
                side_effect=fake_gamry_step,
            ):
                run_protocol_for_sample(
                    run_dir,
                    sample_dir,
                    1,
                    {"sample_id": "sample-1", "label": "Sample 1"},
                    protocol,
                )

            raw = sample_dir / "01_CA.DTA"
            self.assertTrue(raw.is_file())
            self.assertTrue(
                raw.with_name("01_CA_charge_analysis.csv").is_file()
            )
            manifest = load_manifest(run_dir)
            self.assertEqual(
                manifest["analysis_results"][0]["analysis_status"],
                "complete",
            )
            self.assertEqual(
                manifest["trials"][0]["analysis_results"][0]["analysis_type"],
                "cumulative_charge",
            )


class CaChargeProtocolAndLiveTest(unittest.TestCase):
    def base_ca_step(self) -> dict:
        return {
            "name": "ca",
            "technique": "ca",
            "voltage_v": -0.2,
            "duration_s": 2,
            "sample_period_s": 1,
        }

    def test_protocol_round_trip_is_opt_in_and_backward_compatible(self) -> None:
        old = validate_protocol_payload(
            {"protocol_name": "old_ca", "steps": [self.base_ca_step()]}
        )
        self.assertNotIn("analysis", old["steps"][0])

        enabled_step = self.base_ca_step()
        enabled_step["analysis"] = {
            "cumulative_charge": {
                "enabled": True,
                "method": "trapezoidal",
            }
        }
        enabled = validate_protocol_payload(
            {"protocol_name": "new_ca", "steps": [enabled_step]}
        )
        self.assertTrue(
            enabled["steps"][0]["analysis"]["cumulative_charge"]["enabled"]
        )

        disabled_step = self.base_ca_step()
        disabled_step["analysis"] = {
            "cumulative_charge": {
                "enabled": False,
                "method": "trapezoidal",
            }
        }
        disabled = validate_protocol_payload(
            {"protocol_name": "disabled_ca", "steps": [disabled_step]}
        )
        self.assertFalse(
            disabled["steps"][0]["analysis"]["cumulative_charge"]["enabled"]
        )

    def test_ca_range_copies_analysis_to_each_generated_dta(self) -> None:
        protocol = validate_protocol_payload(
            {
                "protocol_name": "ca_range",
                "steps": [
                    {
                        "name": "range",
                        "type": "ca_range",
                        "start_voltage_v": -0.1,
                        "end_voltage_v": -0.3,
                        "step_voltage_v": -0.1,
                        "duration_s": 1,
                        "sample_period_s": 1,
                        "analysis": {
                            "cumulative_charge": {
                                "enabled": True,
                                "method": "trapezoidal",
                            }
                        },
                    }
                ],
            }
        )
        self.assertEqual(len(protocol["steps"]), 3)
        self.assertTrue(
            all(
                step["analysis"]["cumulative_charge"]["enabled"]
                for step in protocol["steps"]
            )
        )

    def test_levich_charge_option_is_explicitly_unsupported(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "Levich"):
            validate_protocol_payload(
                {
                    "protocol_name": "levich_charge",
                    "steps": [
                        {
                            "name": "levich",
                            "technique": "levich_rpm_sweep_ca",
                            "rpm_values": [400, 900],
                            "analysis": {
                                "cumulative_charge": {
                                    "enabled": True,
                                    "method": "trapezoidal",
                                }
                            },
                        }
                    ],
                }
            )

    def test_mock_ca_emits_live_charge_and_final_dta_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            live_dir = root / "_system" / "live"
            initialize_live_stream(live_dir, run_id="run", technique="ca")
            raw = root / "CA.DTA"
            step = self.base_ca_step()
            step["analysis"] = {
                "cumulative_charge": {
                    "enabled": True,
                    "method": "trapezoidal",
                }
            }
            emitter = LiveEmitter(live_dir, mock_time_scale=0)
            result = run_ca(step, raw, emitter=emitter)
            points = read_live_points(live_dir)
            status = read_live_status(live_dir)
            self.assertTrue(points)
            self.assertTrue(all("q_live_c" in point for point in points))
            self.assertEqual(status["charge_analysis_source"], "live estimate")
            final = run_ca_charge_analysis(raw)
            self.assertAlmostEqual(
                result["live_charge_analysis"]["charge_live_c"],
                final["summary"]["result"]["final_signed_charge_c"],
                places=15,
            )

    def test_mock_ca_without_analysis_keeps_original_live_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            live_dir = root / "_system" / "live"
            initialize_live_stream(live_dir, run_id="run", technique="ca")
            emitter = LiveEmitter(live_dir, mock_time_scale=0)
            result = run_ca(self.base_ca_step(), root / "CA.DTA", emitter=emitter)
            self.assertNotIn("live_charge_analysis", result)
            points = read_live_points(live_dir)
            self.assertTrue(points)
            self.assertTrue(all("q_live_c" not in point for point in points))

    def test_mock_ca_staircase_resets_charge_and_tags_each_live_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            live_dir = root / "_system" / "live"
            initialize_live_stream(live_dir, run_id="run", technique="ca_staircase")
            emitter = LiveEmitter(live_dir, mock_time_scale=0)
            step = {
                "name": "staircase",
                "technique": "ca_staircase",
                "start_voltage_v": -0.1,
                "step_voltage_v": -0.1,
                "step_time_s": 1,
                "sample_period_s": 1,
                "analysis": {
                    "cumulative_charge": {
                        "enabled": True,
                        "method": "trapezoidal",
                    }
                },
            }
            run_ca_staircase(
                step,
                [root / "first.DTA", root / "second.DTA"],
                emitter=emitter,
            )
            points = read_live_points(live_dir)
            by_segment = {
                segment: [
                    point
                    for point in points
                    if point["q_live_segment"] == segment
                ]
                for segment in (1, 2)
            }
            self.assertEqual(by_segment[1][0]["q_live_c"], 0)
            self.assertEqual(by_segment[2][0]["q_live_c"], 0)
            self.assertEqual(
                read_live_status(live_dir)["charge_analysis_segment"],
                2,
            )


class CaChargeUiTest(unittest.TestCase):
    def test_builder_live_switch_and_generic_history_controls_are_present(self) -> None:
        page = app.test_client().get("/").get_data(as_text=True)
        self.assertIn("Calculate cumulative signed charge", page)
        self.assertIn('checkbox.className = "echem-analysis-charge"', page)
        self.assertIn('id="liveCurrentPlotBtn"', page)
        self.assertIn('id="liveChargePlotBtn"', page)
        self.assertIn("final result will be recomputed from DTA", page)
        self.assertIn("analysis-series-button", page)
        self.assertIn("/api/current-run/analysis-data", page)
        self.assertIn("CA Range", page)
        self.assertIn("Levich RPM sweep CA in pilot v1", page)

    def test_registered_charge_series_is_available_from_history_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            raw = run_dir / "01_Sample" / "CA.DTA"
            write_mock_ca(raw, [(0, -0.2, 1e-6), (2, -0.2, 1e-6)])
            save_manifest(
                run_dir,
                {
                    "run_id": "run",
                    "outputs": [{"outputs": [str(raw)], "technique": "ca"}],
                    "trials": [],
                    "analysis_results": [],
                },
            )
            analysis = run_ca_charge_analysis(raw)
            registered = register_trial_analysis_result(
                run_dir,
                raw_dta=raw,
                analysis_type=analysis["analysis_type"],
                analysis_version=analysis["analysis_version"],
                label="CA Cumulative Charge",
                technique="ca",
                status="complete",
                analysis_artifacts=analysis["artifacts"],
                summary=analysis["summary"],
            )
            with state.automation_lock:
                original = dict(state.automation_state)
                state.automation_state["run_dir"] = str(run_dir)
            try:
                client = app.test_client()
                listing = client.get("/api/current-run/dta-files")
                self.assertEqual(listing.status_code, 200)
                groups = listing.get_json()["analysis_groups"]
                self.assertEqual(groups[0]["analysis_type"], "cumulative_charge")

                response = client.get(
                    "/api/current-run/analysis-data",
                    query_string={
                        "path": registered["analysis_artifacts"]["series_csv"]
                    },
                )
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["original_point_count"], 2)
                self.assertEqual(payload["source_label"], "Recomputed from DTA")
            finally:
                with state.automation_lock:
                    state.automation_state.clear()
                    state.automation_state.update(original)


if __name__ == "__main__":
    unittest.main()
