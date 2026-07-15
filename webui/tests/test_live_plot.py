from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app import app
from gamry_worker.live_writer import (
    append_live_point,
    fail_live_stream,
    initialize_live_stream,
    read_live_points,
    read_live_status,
)
from gamry_worker.worker import run_job
from workflow import state
from workflow.data_manager import create_sample_workspace


class LivePlotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = app.test_client()
        with state.automation_lock:
            self.original_automation_state = dict(state.automation_state)
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
            state.automation_state.update(self.original_automation_state)

    def set_current_run(self, run_dir: Path) -> None:
        with state.automation_lock:
            state.automation_state["run_dir"] = str(run_dir)

    def test_live_writer_initializes_and_sequences_points(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            live_dir = Path(tmpdir) / "_system" / "live"
            status = initialize_live_stream(
                live_dir,
                run_id="run-1",
                sample_id="sample-1",
                sample_label="Sample 1",
                protocol_name="ocp_only",
                step_name="Open Circuit",
                technique="ocp",
            )
            self.assertTrue(status["active"])
            first = append_live_point(live_dir, {"technique": "ocp", "t_s": 0.1, "e_v": 0.2})
            second = append_live_point(live_dir, {"technique": "ocp", "t_s": 0.2, "e_v": 0.21})
            self.assertEqual([first["seq"], second["seq"]], [1, 2])
            self.assertEqual(read_live_status(live_dir)["point_count"], 2)

    def test_idle_status_and_incremental_points_endpoint(self) -> None:
        response = self.client.get("/api/live/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"]["status"], "idle")

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            live_dir = run_dir / "_system" / "live"
            initialize_live_stream(live_dir, run_id="run", technique="ca")
            append_live_point(live_dir, {"technique": "ca", "t_s": 0.1, "e_v": -0.2, "i_a": 1e-6})
            append_live_point(live_dir, {"technique": "ca", "t_s": 0.2, "e_v": -0.2, "i_a": 2e-6})
            self.set_current_run(run_dir)

            response = self.client.get("/api/live/points?after=1&limit=1")
            payload = response.get_json()
            self.assertEqual(response.status_code, 200)
            self.assertEqual([point["seq"] for point in payload["points"]], [2])

            self.assertEqual(self.client.get("/api/live/points?after=nope").status_code, 400)
            self.assertEqual(self.client.get("/api/live/points?limit=0").status_code, 400)

    def test_partial_final_jsonl_line_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            live_dir = Path(tmpdir)
            initialize_live_stream(live_dir, technique="ocp")
            append_live_point(live_dir, {"technique": "ocp", "t_s": 1, "e_v": 0.1})
            with (live_dir / "points.jsonl").open("a", encoding="utf-8") as stream:
                stream.write('{"seq": 2, "technique": "ocp"')
            points = read_live_points(live_dir, after=0, limit=10)
            self.assertEqual([point["seq"] for point in points], [1])

    def run_live_job(self, technique: str, step: dict, *, scale: float = 0.01) -> tuple[dict, Path, Path]:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        root = Path(tmpdir.name)
        live_dir = root / "_system" / "live"
        output = root / "01_Sample" / f"{technique}.DTA"
        result = run_job(
            {
                "job_id": f"job-{technique}",
                "mode": "mock",
                "run_id": "run-1",
                "sample_id": "sample-1",
                "sample_label": "Sample 1",
                "protocol_name": "test",
                "step": step,
                "outputs": [str(output)],
                "live_dir": str(live_dir),
                "live_enabled": True,
                "gamry": {"live_plot": {"mock_time_scale": scale}},
                "mock_delay_s": 0,
            }
        )
        return result, live_dir, output

    def test_mock_ocp_streams_before_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            live_dir = root / "_system" / "live"
            output = root / "sample" / "ocp.DTA"
            job = {
                "job_id": "job-ocp-live",
                "mode": "mock",
                "run_id": "run-1",
                "sample_id": "sample-1",
                "step": {"name": "OCP", "technique": "ocp", "duration_s": 1, "sample_period_s": 0.1},
                "outputs": [str(output)],
                "live_dir": str(live_dir),
                "live_enabled": True,
                "gamry": {"live_plot": {"mock_time_scale": 0.5}},
                "mock_delay_s": 0,
            }
            worker = threading.Thread(target=run_job, args=(job,))
            worker.start()
            time.sleep(0.12)
            status = read_live_status(live_dir)
            self.assertEqual(status["status"], "running")
            self.assertGreaterEqual(status["point_count"], 1)
            self.assertFalse(output.exists())
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertTrue(output.exists())
            self.assertEqual(read_live_status(live_dir)["status"], "complete")

    def test_mock_ca_cv_and_eis_live_shapes(self) -> None:
        _, ca_live, _ = self.run_live_job(
            "ca",
            {"name": "CA", "technique": "ca", "voltage_v": -0.2, "duration_s": 0.2, "sample_period_s": 0.1},
        )
        ca_point = read_live_points(ca_live, limit=10)[0]
        self.assertIn("i_a", ca_point)
        self.assertIn("e_v", ca_point)

        _, cv_live, _ = self.run_live_job(
            "cv",
            {
                "name": "CV",
                "technique": "cv",
                "initial_voltage_v": 0,
                "first_vertex_v": 0.1,
                "second_vertex_v": -0.1,
                "final_voltage_v": 0,
                "scan_rate_v_s": 1,
                "sample_period_s": 0.05,
            },
        )
        self.assertEqual(read_live_points(cv_live, limit=1)[0]["technique"], "cv")

        _, eis_live, _ = self.run_live_job(
            "eis",
            {"name": "EIS", "technique": "eis", "initial_frequency_hz": 1000, "final_frequency_hz": 10, "points_per_decade": 2},
        )
        eis_point = read_live_points(eis_live, limit=10)[0]
        self.assertEqual(eis_point["technique"], "eis")
        self.assertIn("zreal_ohm", eis_point)
        self.assertIn("zimag_ohm", eis_point)

    def test_new_step_resets_stream_and_failure_updates_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            live_dir = Path(tmpdir)
            job_base = {
                "job_id": "reset",
                "mode": "mock",
                "run_id": "run-1",
                "live_dir": str(live_dir),
                "live_enabled": True,
                "gamry": {"live_plot": {"mock_time_scale": 0}},
                "mock_delay_s": 0,
            }
            run_job({**job_base, "step": {"name": "first", "technique": "ocp", "duration_s": 0, "sample_period_s": 1}, "outputs": [str(Path(tmpdir) / "first.DTA")]})
            run_job({**job_base, "step": {"name": "second", "technique": "ocp", "duration_s": 0, "sample_period_s": 1}, "outputs": [str(Path(tmpdir) / "second.DTA")]})
            status = read_live_status(live_dir)
            self.assertEqual(status["step_name"], "second")
            self.assertEqual(status["point_count"], 1)
            fail_live_stream(live_dir, "aborted by test", status="aborted")
            self.assertEqual(read_live_status(live_dir)["status"], "aborted")
            self.assertEqual(read_live_status(live_dir)["error"], "aborted by test")

    def test_dta_behavior_and_sample_folder_remain_clean(self) -> None:
        result, live_dir, output = self.run_live_job(
            "ocp",
            {"name": "OCP", "technique": "ocp", "duration_s": 0, "sample_period_s": 1},
        )
        self.assertTrue(result["ok"])
        self.assertTrue(output.exists())
        self.assertEqual(output.suffix, ".DTA")
        self.assertTrue((live_dir / "status.json").exists())
        self.assertTrue((live_dir / "points.jsonl").exists())
        self.assertEqual(list(output.parent.rglob("*")), [output])

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            run_dir.mkdir()
            sample_dir = create_sample_workspace(
                run_dir,
                {"sample_id": "sample-1", "label": "Sample 1"},
                1,
            )
            self.assertEqual([path.name for path in sample_dir.iterdir()], [])
            self.assertFalse((sample_dir / "_system").exists())


if __name__ == "__main__":
    unittest.main()
