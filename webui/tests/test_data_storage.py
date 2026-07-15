from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hardware.gamry_client import GamryClient
from workflow.data_manager import (
    build_step_outputs,
    create_run_workspace,
    create_sample_workspace,
    mark_run_complete,
)


class DataStorageTest(unittest.TestCase):
    def test_run_keeps_dta_files_user_facing_and_metadata_internal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_plan = {
                "run_name": "storage_test",
                "display_name": "Storage Test",
            }

            with patch("workflow.data_manager.output_root", return_value=root):
                workspace = create_run_workspace(run_plan)

            run_dir = workspace["run_dir"]
            sample = {"sample_id": "sample_001", "label": "Gold Electrode"}
            sample_dir = create_sample_workspace(run_dir, sample, sample_index=1)
            outputs = build_step_outputs(
                sample_dir,
                {"technique": "ocp", "name": "OCP before"},
                step_index=1,
            )
            Path(outputs[0]).write_text("TEST DTA\n", encoding="utf-8")
            mark_run_complete(run_dir)

            self.assertEqual(sample_dir.parent, run_dir)
            self.assertEqual(Path(outputs[0]).parent, sample_dir)
            self.assertTrue((run_dir / "README_DATA.txt").is_file())
            self.assertTrue((run_dir / "run.log").is_file())
            self.assertTrue((run_dir / "_system" / "manifest.json").is_file())
            self.assertFalse((sample_dir / "sample.json").exists())

            summary = json.loads(
                (run_dir / "run_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["status"], "complete")
            self.assertEqual(len(summary["dta_files"]), 1)

    def test_worker_jobs_are_stored_under_system_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = GamryClient().job_dir(tmpdir)

            self.assertEqual(path, Path(tmpdir) / "_system" / "jobs")
            self.assertTrue(path.is_dir())


if __name__ == "__main__":
    unittest.main()
