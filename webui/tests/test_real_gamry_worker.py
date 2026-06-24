from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from gamry_worker.worker import run_job


class RealGamryWorkerTest(unittest.TestCase):
    def write_runner(self, tmpdir: str) -> Path:
        runner_path = Path(tmpdir) / "fake_real_runner.py"
        runner_path.write_text(
            textwrap.dedent(
                """
                from __future__ import annotations

                import argparse
                import json
                from pathlib import Path

                parser = argparse.ArgumentParser()
                parser.add_argument("--job", required=True)
                parser.add_argument("--result", required=True)
                args = parser.parse_args()

                with open(args.job, "r", encoding="utf-8") as f:
                    job = json.load(f)

                for output in job["outputs"]:
                    output_path = Path(output)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text("real runner output\\n", encoding="utf-8")

                result = {
                    "ok": True,
                    "job_id": job["job_id"],
                    "instrument": "fake Windows Gamry",
                }
                Path(args.result).write_text(json.dumps(result), encoding="utf-8")
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return runner_path

    def test_real_mode_runs_external_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner_path = self.write_runner(tmpdir)
            job_path = Path(tmpdir) / "job.json"
            result_path = Path(tmpdir) / "result.json"
            output_path = Path(tmpdir) / "sample_001_ocp.DTA"
            job = {
                "job_id": "test_real_ocp",
                "mode": "real",
                "sample_id": "sample_001",
                "step": {
                    "name": "ocp",
                    "technique": "ocp",
                    "duration_s": 1,
                    "sample_period_s": 1,
                },
                "outputs": [str(output_path)],
                "result_path": str(result_path),
                "_job_path": str(job_path),
                "gamry": {
                    "real_worker_python": sys.executable,
                    "real_worker_script": str(runner_path),
                    "real_timeout_s": 5,
                },
            }
            job_path.write_text(json.dumps(job), encoding="utf-8")

            result = run_job(job)

            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "real")
            self.assertTrue(output_path.exists())
            self.assertEqual(result["result"]["backend"], "external")
            self.assertEqual(result["result"]["runner"]["instrument"], "fake Windows Gamry")

    def test_real_mode_requires_configured_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            job_path = Path(tmpdir) / "job.json"
            output_path = Path(tmpdir) / "sample_001_ocp.DTA"
            job = {
                "job_id": "test_missing_real_runner",
                "mode": "real",
                "sample_id": "sample_001",
                "step": {
                    "name": "ocp",
                    "technique": "ocp",
                },
                "outputs": [str(output_path)],
                "_job_path": str(job_path),
                "gamry": {},
            }
            job_path.write_text(json.dumps(job), encoding="utf-8")

            with self.assertRaises(Exception) as context:
                run_job(job)

            self.assertIn("real_worker_script", str(context.exception))


if __name__ == "__main__":
    unittest.main()
