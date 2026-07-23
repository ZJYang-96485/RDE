from __future__ import annotations

import unittest

from app import app
from workflow.state import get_axis_position, set_axis_position


class TrackedPositionApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = app.test_client()
        set_axis_position("linear", -35000)

    def tearDown(self) -> None:
        set_axis_position("linear", 0)

    def test_confirmation_is_required(self) -> None:
        response = self.client.post(
            "/api/axes/tracked-position",
            json={"axis": "z", "position": 0},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(get_axis_position("linear"), -35000)

    def test_z_record_is_corrected_without_hardware_command(self) -> None:
        response = self.client.post(
            "/api/axes/tracked-position",
            json={
                "axis": "z",
                "position": 0,
                "confirm_software_only": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["position"], 0)
        self.assertFalse(payload["hardware_command_sent"])
        self.assertEqual(get_axis_position("linear"), 0)


if __name__ == "__main__":
    unittest.main()
