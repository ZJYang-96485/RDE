from __future__ import annotations

import unittest

from app import app


class CollapsibleBuildersUiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.page = app.test_client().get("/").get_data(as_text=True)

    def test_run_plan_groups_have_compact_accessible_collapse_controls(self) -> None:
        self.assertIn(
            'class="group-collapse home-btn" type="button" aria-expanded="true"',
            self.page,
        )
        self.assertIn("function setGroupCollapsed(groupBlock, collapsed)", self.page)
        self.assertIn(
            ".recipe-group.is-collapsed > :not(.recipe-group-header)",
            self.page,
        )
        self.assertIn(
            'disabled && !el.classList.contains("group-collapse")',
            self.page,
        )

    def test_echem_steps_have_compact_accessible_collapse_controls(self) -> None:
        self.assertIn(
            'class="echem-step-collapse home-btn" type="button" aria-expanded="true"',
            self.page,
        )
        self.assertIn("function setEchemStepCollapsed(stepBlock, collapsed)", self.page)
        self.assertIn(
            ".echem-step.is-collapsed > :not(.echem-step-header)",
            self.page,
        )
        self.assertIn(
            'disabled && !el.classList.contains("echem-step-collapse")',
            self.page,
        )


if __name__ == "__main__":
    unittest.main()
