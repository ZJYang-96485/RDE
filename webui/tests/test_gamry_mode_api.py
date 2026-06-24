from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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

    def test_can_select_real_then_mock_mode(self) -> None:
        response = self.client.post("/api/config/gamry-mode", json={"mode": "real"})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["gamry_mode"], "real")
        self.assertEqual(config_loader.load_config(refresh=True)["gamry"]["mode"], "real")

        response = self.client.post("/api/config/gamry-mode", json={"mode": "mock"})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["gamry_mode"], "mock")
        self.assertEqual(config_loader.load_config(refresh=True)["gamry"]["mode"], "mock")

    def test_rejects_unknown_gamry_mode(self) -> None:
        response = self.client.post("/api/config/gamry-mode", json={"mode": "banana"})

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
