from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import app
from workflow import state
from workflow.data_manager import export_run_dta_to_csv, save_manifest
from workflow.dta_viewer import DtaViewerError, TECHNIQUE_PLOT_SPECS, parse_dta_file


class DtaViewerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = app.test_client()
        with state.automation_lock:
            self.original_state = dict(state.automation_state)
            state.automation_state.update(
                {
                    "running": False,
                    "step": "Idle",
                    "error": None,
                    "run_dir": None,
                    "started_at": None,
                    "finished_at": None,
                }
            )

    def tearDown(self) -> None:
        with state.automation_lock:
            state.automation_state.clear()
            state.automation_state.update(self.original_state)

    def set_current_run(self, run_dir: Path, running: bool = False) -> None:
        with state.automation_lock:
            state.automation_state["run_dir"] = str(run_dir)
            state.automation_state["running"] = running

    @staticmethod
    def write_dta(run_dir: Path, relative_path: str, body: str) -> Path:
        path = run_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def test_no_current_run_returns_empty_list(self) -> None:
        response = self.client.get("/api/current-run/dta-files")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["run_id"])
        self.assertEqual(payload["files"], [])

    def test_list_is_restricted_to_current_session_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            current = root / "20260720-153012_Test_Run"
            old = root / "20260719-120000_Old_Run"
            self.write_dta(
                current,
                "01_Sample_1/003_01_OCP.DTA",
                "TECHNIQUE\tocp\ntime_s\tpotential_v\n0\t0.12\n",
            )
            self.write_dta(
                old,
                "01_Old/old.DTA",
                "TECHNIQUE\tocp\ntime_s\tpotential_v\n0\t9.9\n",
            )
            self.set_current_run(current, running=True)

            response = self.client.get("/api/current-run/dta-files")
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["run_id"], current.name)
            self.assertEqual(len(payload["files"]), 1)
            record = payload["files"][0]
            self.assertEqual(record["relative_path"], "01_Sample_1/003_01_OCP.DTA")
            self.assertEqual(record["label"], "01_Sample_1 / 003_01_OCP.DTA")
            self.assertNotIn("old.DTA", str(payload))

            # The same run remains available after automation completes.
            self.set_current_run(current, running=False)
            completed = self.client.get("/api/current-run/dta-files").get_json()
            self.assertEqual(completed["run_id"], current.name)
            self.assertEqual(len(completed["files"]), 1)

    def test_ocp_ca_cv_cp_and_eis_plot_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixtures = {
                "OCP.DTA": (
                    "TECHNIQUE\tocp\ntime_s\tpotential_v\n0\t0.12\n1\t0.13\n",
                    "Time (s)",
                    "Potential (V)",
                    {"x": 1.0, "y": 0.13},
                ),
                "CA.DTA": (
                    "TECHNIQUE\tca\ntime_s\tapplied_voltage_v\tcurrent_a\n0\t0.1\t-1e-6\n",
                    "Time (s)",
                    "Current (A)",
                    {"x": 0.0, "y": -1e-6},
                ),
                "CV.DTA": (
                    "TECHNIQUE\tcv\ntime_s\tpotential_v\tcurrent_a\n0\t0.2\t3e-6\n",
                    "Potential (V)",
                    "Current (A)",
                    {"x": 0.2, "y": 3e-6},
                ),
                "CP.DTA": (
                    "TECHNIQUE\tcp\nPt\tT\tVf\tIm\tQ_Ah\n0\t2\t0.4\t-2e-5\t0\n",
                    "Time (s)",
                    "Potential (V)",
                    {"x": 2.0, "y": 0.4},
                ),
                "EIS.DTA": (
                    "TECHNIQUE\teis\nFreq\tZreal\tZimag\n100\t20\t-5\n",
                    "Zreal (ohm)",
                    "-Zimag (ohm)",
                    {"x": 20.0, "y": 5.0},
                ),
            }

            for filename, (body, x_label, y_label, expected_point) in fixtures.items():
                with self.subTest(filename=filename):
                    path = self.write_dta(root, filename, body)
                    parsed = parse_dta_file(path)
                    self.assertEqual(parsed["x_label"], x_label)
                    self.assertEqual(parsed["y_label"], y_label)
                    self.assertEqual(parsed["points"][-1], expected_point)

    def test_history_lists_and_serves_automatic_csv_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "run"
            dta = self.write_dta(
                root,
                "01_Sample/OCP.DTA",
                "TECHNIQUE\tocp\ntime_s\tpotential_v\n0\t0.12\n1\t0.13\n",
            )
            save_manifest(
                root,
                {
                    "outputs": [{"outputs": [str(dta)]}],
                    "trials": [],
                    "analysis_results": [],
                },
            )
            export_run_dta_to_csv(root)
            self.set_current_run(root)

            payload = self.client.get("/api/current-run/dta-files").get_json()
            self.assertEqual(payload["files"][0]["csv_relative_path"], "01_Sample/OCP.csv")
            response = self.client.get(
                "/api/current-run/history-artifact?path=01_Sample/OCP.csv&download=1"
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"time_s,potential_v", response.data)
            response.close()

    def test_technique_plot_specs_define_final_x_and_y_axes(self) -> None:
        expected = {
            "ocp": ("time", "potential"),
            "ca": ("time", "current"),
            "cp": ("time", "potential"),
            "cc_charge": ("time", "potential"),
            "cc_discharge": ("time", "potential"),
            "cv": ("potential", "current"),
            "lsv": ("potential", "current"),
            "eis": ("zreal", "zimag"),
            "geis": ("zreal", "zimag"),
        }
        for technique, axes in expected.items():
            with self.subTest(technique=technique):
                spec = TECHNIQUE_PLOT_SPECS[technique]
                self.assertEqual((spec["x"], spec["y"]), axes)

    def test_known_technique_never_falls_back_to_wrong_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "OCP.DTA"
            path.write_text("TECHNIQUE\tocp\nPt\tT\tLabel\n0\t0.5\t1\n", encoding="utf-8")
            with self.assertRaisesRegex(DtaViewerError, "OCP plotting requires time and potential"):
                parse_dta_file(path)

    def test_real_gamry_metadata_time_row_is_not_used_as_curve_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "005_01_OCP.DTA"
            path.write_text(
                "EXPLAIN\n"
                "TAG\tCORPOT\n"
                "DATE\tLABEL\t07/21/2026\tDate\n"
                "TIME\tLABEL\t13:58:39\tTime\n"
                "CURVE\tTABLE\t2\n"
                "\tPt\tT\tVf\tVm\tAch\tOver\tTemp\t\n"
                "\t#\ts\tV vs. Ref.\tV\tA\tbits\tdeg C\t\n"
                "\t0\t0.5\t0.2036855\t0.2036855\t0\t...........\t2.7671\t\n"
                "\t1\t1.0\t0.2036782\t0.2033014\t0\t...........\t2.7671\t\n",
                encoding="utf-8",
            )

            parsed = parse_dta_file(path)
            self.assertEqual(parsed["technique_guess"], "ocp")
            self.assertEqual(parsed["x_label"], "Time (s)")
            self.assertEqual(parsed["y_label"], "Potential (V)")
            self.assertEqual(parsed["points"][0], {"x": 0.5, "y": 0.2036855})

    def test_data_endpoint_rejects_traversal_absolute_and_unlisted_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "run"
            self.write_dta(
                root,
                "01_Sample/OCP.DTA",
                "TECHNIQUE\tocp\ntime_s\tpotential_v\n0\t0.1\n",
            )
            self.set_current_run(root)

            traversal = self.client.get("/api/current-run/dta-data?path=../config.json")
            self.assertEqual(traversal.status_code, 400)

            absolute = self.client.get("/api/current-run/dta-data?path=C:/Windows/test.DTA")
            self.assertEqual(absolute.status_code, 400)

            missing = self.client.get("/api/current-run/dta-data?path=01_Sample/missing.DTA")
            self.assertEqual(missing.status_code, 404)

            valid = self.client.get("/api/current-run/dta-data?path=01_Sample/OCP.DTA")
            self.assertEqual(valid.status_code, 200)
            payload = valid.get_json()
            self.assertEqual(payload["technique_guess"], "ocp")
            self.assertEqual(payload["relative_path"], "01_Sample/OCP.DTA")

    def test_large_tables_are_evenly_decimated_to_5000_points(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "OCP.DTA"
            rows = ["TECHNIQUE\tocp", "time_s\tpotential_v"]
            rows.extend(f"{index}\t{index / 1000}" for index in range(6001))
            path.write_text("\n".join(rows), encoding="utf-8")

            parsed = parse_dta_file(path)
            self.assertEqual(parsed["point_count"], 5000)
            self.assertEqual(parsed["original_point_count"], 6001)
            self.assertTrue(parsed["decimated"])
            self.assertEqual(parsed["points"][0]["x"], 0.0)
            self.assertEqual(parsed["points"][-1]["x"], 6000.0)

    def test_unknown_columns_fall_back_to_first_two_numeric_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "unfamiliar.DTA"
            path.write_text("custom_x\tcustom_y\tcustom_z\n1\t2\t3\n4\t5\t6\n", encoding="utf-8")
            parsed = parse_dta_file(path)
            self.assertEqual(parsed["technique_guess"], "auto")
            self.assertEqual(parsed["x_label"], "Auto-detected: custom_x")
            self.assertEqual(parsed["y_label"], "Auto-detected: custom_y")
            self.assertEqual(parsed["points"][-1], {"x": 4.0, "y": 5.0})


class DtaViewerUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            Path(__file__).resolve().parents[1] / "templates" / "index.html"
        ).read_text(encoding="utf-8")
        cls.page = app.test_client().get("/").get_data(as_text=True)

    def test_same_trial_panel_has_required_controls_and_canvas(self) -> None:
        self.assertIn("Same Trial EChem Figures", self.page)
        self.assertIn('id="sameTrialRefreshBtn"', self.page)
        self.assertIn('id="sameTrialBackLiveBtn"', self.page)
        self.assertIn('id="sameTrialFileList"', self.page)
        self.assertIn('id="sameTrialCanvas"', self.page)
        self.assertIn("Showing DTA files and their automatic CSV exports", self.page)

    def test_saved_figure_view_does_not_pause_live_collection(self) -> None:
        self.assertIn("Viewing saved DTA file", self.source)
        self.assertIn("Viewing live measurement", self.source)
        self.assertIn("Live collection continues independently", self.source)
        load_start = self.source.index("async function loadSameTrialFigure")
        load_end = self.source.index("function updateLiveMeta", load_start)
        load_block = self.source[load_start:load_end]
        self.assertNotIn("livePaused =", load_block)
        self.assertNotIn("scheduleLivePoll", load_block)

    def test_viewer_polls_only_while_automation_runs(self) -> None:
        self.assertIn("if (automationRunning) {\n          refreshSameTrialFigures", self.source)
        self.assertIn("}, 3000);", self.source)
        self.assertIn('fetch("/api/current-run/dta-files")', self.source)
        self.assertIn("/api/current-run/dta-data?path=", self.source)

    def test_canvas_plot_is_vanilla_javascript(self) -> None:
        self.assertIn("function drawSameTrialPlot", self.source)
        self.assertIn("formatSameTrialNumber", self.source)
        self.assertNotIn("cdn.jsdelivr.net", self.page)
        self.assertNotIn("cdnjs.cloudflare.com", self.page)


if __name__ == "__main__":
    unittest.main()
