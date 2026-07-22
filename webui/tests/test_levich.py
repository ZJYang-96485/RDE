from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workflow.data_manager import (
    register_analysis_result,
    save_manifest,
    write_run_summary,
)
from workflow.history_artifacts import list_analysis_groups, resolve_registered_artifact
from workflow.levich_analysis import run_levich_analysis
from workflow.protocol_loader import ProtocolError, validate_protocol_payload


class LevichProtocolTest(unittest.TestCase):
    def test_protocol_normalizes_commanded_rpm_continuous_ca(self) -> None:
        protocol = validate_protocol_payload(
            {
                "protocol_name": "levich",
                "steps": [
                    {
                        "name": "Levich sweep",
                        "technique": "levich_rpm_sweep_ca",
                        "rpm_values": "400, 900, 1600, 2500",
                        "pre_stabilization_s": 5,
                        "stabilization_s": 3,
                        "collection_s": 10,
                        "sample_period_s": 1,
                        "voltage_v": -0.4,
                    }
                ],
            }
        )
        step = protocol["steps"][0]
        self.assertEqual(step["rpm_values"], [400, 900, 1600, 2500])
        self.assertEqual(step["output"], "Levich_CA_sweep.DTA")
        self.assertEqual(step["rpm_source"], "commanded")
        self.assertEqual(step["stabilization_mode"], "fixed delay")
        self.assertGreater(step["duration_s"], 40)

    def test_protocol_rejects_duplicate_or_out_of_range_rpm(self) -> None:
        base = {
            "protocol_name": "levich",
            "steps": [
                {
                    "name": "Levich sweep",
                    "technique": "levich_rpm_sweep_ca",
                    "rpm_values": [400, 400],
                }
            ],
        }
        with self.assertRaisesRegex(ProtocolError, "duplicate"):
            validate_protocol_payload(base)
        base["steps"][0]["rpm_values"] = [20, 400]
        with self.assertRaisesRegex(ProtocolError, "between"):
            validate_protocol_payload(base)


class LevichAnalysisTest(unittest.TestCase):
    def write_fixture(self, root: Path) -> tuple[Path, Path]:
        raw = root / "004_01_Levich_CA_sweep.DTA"
        lines = [
            "MOCK_GAMRY_DATA",
            "TECHNIQUE\tca",
            "time_s\tcurrent_a",
        ]
        for elapsed in range(10):
            current = -1e-5 if elapsed < 5 else -2e-5
            lines.append(f"{elapsed}\t{current}")
        raw.write_text("\n".join(lines) + "\n", encoding="utf-8")
        schedule = root / "004_01_Levich_CA_sweep_rpm_schedule.json"
        schedule.write_text(
            json.dumps(
                {
                    "technique": "levich_rpm_sweep_ca",
                    "label": "Levich CA RPM Sweep",
                    "rpm_source": "commanded",
                    "stabilization_mode": "fixed delay",
                    "rpm_points": [
                        {
                            "index": 1,
                            "commanded_rpm": 400,
                            "collection_start_s": 0,
                            "collection_end_s": 4,
                        },
                        {
                            "index": 2,
                            "commanded_rpm": 1600,
                            "collection_start_s": 5,
                            "collection_end_s": 9,
                        },
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return raw, schedule

    def test_post_run_analysis_creates_csv_json_and_three_pngs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw, schedule = self.write_fixture(root)
            result = run_levich_analysis(raw, schedule, area_cm2=0.5)
            artifacts = result["artifacts"]
            self.assertEqual(result["analysis"]["rpm_source"], "commanded")
            self.assertEqual(result["analysis"]["stabilization_mode"], "fixed delay")
            self.assertEqual(len(result["analysis"]["rpm_points"]), 2)
            for key in (
                "summary_csv",
                "analysis_json",
                "trace_plot_png",
                "levich_plot_png",
                "kl_plot_png",
            ):
                self.assertTrue(artifacts[key].is_file(), key)
            for key in ("trace_plot_png", "levich_plot_png", "kl_plot_png"):
                self.assertEqual(artifacts[key].read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertIn("commanded", artifacts["summary_csv"].read_text(encoding="utf-8"))

    def test_registered_analysis_is_grouped_and_resolvable_for_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            sample_dir = run_dir / "01_Sample"
            sample_dir.mkdir(parents=True)
            raw, schedule = self.write_fixture(sample_dir)
            analysis = run_levich_analysis(raw, schedule)
            save_manifest(
                run_dir,
                {
                    "run_id": "run",
                    "status": "running",
                    "samples": [],
                    "outputs": [{"outputs": [str(raw)], "technique": "levich_rpm_sweep_ca"}],
                    "analysis_results": [],
                    "errors": [],
                },
            )
            registered = register_analysis_result(
                run_dir,
                raw_dta=raw,
                analysis_artifacts=analysis["artifacts"],
            )
            write_run_summary(run_dir)
            groups = list_analysis_groups(run_dir)
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["label"], "Levich CA RPM Sweep")
            self.assertEqual(len(groups[0]["artifacts"]), 6)
            resolved = resolve_registered_artifact(
                run_dir,
                registered["analysis_artifacts"]["levich_plot_png"],
            )
            self.assertEqual(resolved.name, "004_01_Levich_CA_sweep_levich_plot.png")
            summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["analysis_results"][0]["rpm_source"], "commanded")


if __name__ == "__main__":
    unittest.main()
