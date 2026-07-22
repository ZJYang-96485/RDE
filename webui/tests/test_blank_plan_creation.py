from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow.protocol_loader import (
    ProtocolError,
    create_blank_echem_protocol,
    list_protocols,
    save_protocol,
)
from workflow.run_plan_loader import (
    RunPlanError,
    create_blank_sample_run_plan,
    list_run_plans,
    save_run_plan,
)


class BlankPlanCreationTests(unittest.TestCase):
    def test_new_blank_sample_run_plan_contains_no_copied_data(self) -> None:
        first = create_blank_sample_run_plan()
        first["groups"].append({"label": "changed"})
        second = create_blank_sample_run_plan()
        self.assertEqual(second["name"], "")
        self.assertEqual(second["groups"], [])
        self.assertEqual(second["steps"], [])
        self.assertEqual(second["editor_mode"], "create")

    def test_new_blank_echem_protocol_contains_no_copied_data(self) -> None:
        first = create_blank_echem_protocol()
        first["steps"].append({"technique": "cv"})
        second = create_blank_echem_protocol()
        self.assertIsNone(second["technique"])
        self.assertEqual(second["parameters"], {})
        self.assertEqual(second["steps"], [])

    def test_duplicate_run_plan_name_is_rejected_and_new_plan_is_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            payload = {
                "schema_version": 2,
                "run_name": "Unique plan",
                "display_name": "Unique plan",
                "repetitions": 1,
                "groups": [],
            }
            with patch("workflow.run_plan_loader.run_plans_dir", return_value=root):
                save_run_plan(payload, overwrite=False)
                self.assertIn("Unique plan", [item["run_name"] for item in list_run_plans()])
                with self.assertRaisesRegex(RunPlanError, "already exists"):
                    save_run_plan(payload, overwrite=False)

    def test_duplicate_protocol_name_is_rejected_and_new_protocol_is_listed(self) -> None:
        source = Path(__file__).resolve().parents[1] / "protocols" / "ocp_only.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["protocol_name"] = "unique_protocol"
        payload["display_name"] = "Unique Protocol"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("workflow.protocol_loader.protocols_dir", return_value=root):
                save_protocol(payload, overwrite=False)
                self.assertIn("unique_protocol", [item["protocol_name"] for item in list_protocols()])
                with self.assertRaisesRegex(ProtocolError, "already exists"):
                    save_protocol(payload, overwrite=False)


if __name__ == "__main__":
    unittest.main()
