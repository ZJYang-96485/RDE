from __future__ import annotations

import unittest

from app import app


class ProtocolApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = app.test_client()

    def test_protocol_without_name_returns_default(self) -> None:
        response = self.client.get("/api/protocol")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["protocol_name"], "ocp_only")

    def test_missing_named_protocol_returns_error(self) -> None:
        response = self.client.get("/api/protocol?name=does_not_exist")

        self.assertEqual(response.status_code, 404)
        payload = response.get_json()
        self.assertIn("error", payload)
        self.assertIn("does_not_exist", payload["error"])


if __name__ == "__main__":
    unittest.main()
