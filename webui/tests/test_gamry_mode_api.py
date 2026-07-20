from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app
from workflow import config_loader


class GamryModeApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = app.test_client()
        self.original_config_path = config_loader.CONFIG_PATH
        self.tempdir = tempfile.TemporaryDirectory()
        self.temp_config_path = Path(self.tempdir.name) / "config.json"
        self.temp_config_path.write_text(
            self.original_config_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        config_loader.CONFIG_PATH = self.temp_config_path
        config_loader.reload_config()

    def tearDown(self) -> None:
        config_loader.CONFIG_PATH = self.original_config_path
        config_loader.reload_config()
        self.tempdir.cleanup()

    def test_backend_is_read_only_from_config_and_serial_map_is_unchanged(self) -> None:
        original_text = self.temp_config_path.read_text(encoding="utf-8")
        original_ports = config_loader.load_config(refresh=True)["serial"]["ports"]

        response = self.client.post("/api/config/gamry-mode", json={"mode": "real"})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["gamry_mode"], "real")
        self.assertEqual(config_loader.load_config(refresh=True)["gamry"]["mode"], "real")
        self.assertEqual(self.temp_config_path.read_text(encoding="utf-8"), original_text)
        self.assertEqual(config_loader.load_config()["serial"]["ports"], original_ports)

        response = self.client.post("/api/config/gamry-mode", json={"mode": "mock"})

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertIn("fixed by config.json", payload["error"])
        self.assertEqual(self.temp_config_path.read_text(encoding="utf-8"), original_text)
        self.assertEqual(config_loader.load_config(refresh=True)["serial"]["ports"], original_ports)

    def test_rejects_unknown_gamry_mode(self) -> None:
        response = self.client.post("/api/config/gamry-mode", json={"mode": "banana"})

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertIn("error", payload)

    @patch("app.get_gamry_client")
    def test_config_reports_real_worker_runtime(self, get_client) -> None:
        get_client.return_value.runtime_status.return_value = {
            "configured": True,
            "worker_python": "gamry-python",
            "worker_python_exists": True,
            "worker_script": "gamry_worker/worker.py",
            "worker_script_exists": True,
        }

        response = self.client.get("/api/config")

        self.assertEqual(response.status_code, 200)
        config = response.get_json()["config"]
        self.assertTrue(config["gamry_real_runner_configured"])
        self.assertEqual(config["gamry_instrument_label"], "IFC1010-36030")
        self.assertTrue(config["gamry_runtime"]["worker_python_exists"])
        self.assertTrue(config["gamry_runtime"]["worker_script_exists"])

    @patch("app.get_gamry_client")
    def test_probe_endpoint_returns_detected_instrument(self, get_client) -> None:
        get_client.return_value.probe.return_value = {
            "ok": True,
            "connected": True,
            "sections": ["IFC1010-36030"],
            "selected_instrument": "IFC1010-36030",
        }

        response = self.client.post("/api/gamry/probe")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["probe"]["connected"])
        self.assertEqual(payload["probe"]["selected_instrument"], "IFC1010-36030")


if __name__ == "__main__":
    unittest.main()
