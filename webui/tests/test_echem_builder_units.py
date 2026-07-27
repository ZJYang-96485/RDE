from __future__ import annotations

import unittest
from pathlib import Path

from app import app


class EchemBuilderUnitsUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            Path(__file__).resolve().parents[1] / "templates" / "index.html"
        ).read_text(encoding="utf-8")
        cls.page = app.test_client().get("/").get_data(as_text=True)

    def test_common_wet_lab_units_are_explicit(self) -> None:
        for label in (
            "Scan rate (mV/s)",
            "Potential step (mV)",
            "Applied current (mA, signed; + is anodic)",
            "Current magnitude (mA, positive)",
            "Expected maximum |current| (mA)",
            "Capacity cutoff (mAh, optional)",
            "AC current amplitude (mA RMS)",
            "DC current (mA, signed)",
        ):
            self.assertIn(label, self.page)

    def test_scaled_fields_keep_si_protocol_keys(self) -> None:
        expected_scaled_keys = (
            "scan_rate_v_s",
            "step_size_v",
            "current_a",
            "expected_max_current_a",
            "capacity_cutoff_ah",
            "ac_current_a",
            "dc_current_a",
        )
        for key in expected_scaled_keys:
            self.assertRegex(
                self.source,
                rf'key: "{key}".*displayScale: 1000',
            )

        self.assertIn("function echemFieldDisplayValue(spec, internalValue)", self.source)
        self.assertIn("numeric * spec.displayScale", self.source)
        self.assertIn("function echemFieldInternalValue(spec, displayValue)", self.source)
        self.assertIn("displayValue / spec.displayScale", self.source)
        self.assertIn(
            "payload[spec.key] = echemFieldInternalValue(spec, displayValue);",
            self.source,
        )

    def test_builder_explains_display_and_worker_units(self) -> None:
        self.assertIn(
            "The builder uses common wet-lab units (mV/s, mV, mA, and mAh where appropriate).",
            self.page,
        )
        self.assertIn(
            "Saved protocols and Gamry workers are converted automatically to V/s, V, A, and Ah.",
            self.page,
        )


if __name__ == "__main__":
    unittest.main()
