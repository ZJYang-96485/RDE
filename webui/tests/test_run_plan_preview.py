from __future__ import annotations

import unittest
from pathlib import Path

from app import app


class RunPlanPreviewUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            Path(__file__).resolve().parents[1] / "templates" / "index.html"
        ).read_text(encoding="utf-8")
        cls.page = app.test_client().get("/").get_data(as_text=True)

    def test_preview_panel_and_human_friendly_icons_are_rendered(self) -> None:
        self.assertIn('id="runPlanTimeline"', self.page)
        self.assertIn('id="runPlanStepDetail"', self.page)
        self.assertIn('id="runPlanReturnStatus"', self.page)
        self.assertIn('id="runPlanWarnings"', self.page)
        self.assertIn("const runPlanIconPaths = {", self.page)
        self.assertIn('motionX: \'<path d="M3 12h18', self.page)
        self.assertIn('rinse: \'<path d="M12 3s6', self.page)
        self.assertIn('echem: \'<path d="M3 18h18', self.page)
        self.assertNotIn("cdnjs.cloudflare.com", self.page)

    def test_preview_is_after_the_builder_and_never_parallel(self) -> None:
        builder_position = self.page.index('class="run-plan-builder-column"')
        preview_position = self.page.index('id="runPlanPreviewPanel"')
        self.assertLess(builder_position, preview_position)
        self.assertIn(".run-plan-layout {\n        display: block;", self.source)
        self.assertIn("position: static;", self.source)
        self.assertNotIn("grid-template-columns: minmax(0, 1.2fr)", self.source)

    def test_preview_and_timeline_groups_are_collapsible(self) -> None:
        self.assertIn('id="runPlanPreviewCollapseBtn"', self.page)
        self.assertIn("function setRunPlanPreviewCollapsed(collapsed)", self.source)
        self.assertIn("const collapsedRunPlanPreviewGroups = new Set()", self.source)
        self.assertIn('class="preview-group-label preview-group-toggle"', self.source)
        self.assertIn('class="preview-group-steps"', self.source)

    def test_z_direction_is_explicit_and_matches_hardware_convention(self) -> None:
        self.assertIn("motionZDown:", self.source)
        self.assertIn("motionZUp:", self.source)
        self.assertIn('if (steps > 0) return "Down"', self.source)
        self.assertIn('if (steps < 0) return "Up"', self.source)
        self.assertIn("later.step.steps > 0", self.source)
        self.assertIn("state.z > 0", self.source)
        self.assertIn("Z signed relative steps (+ = Down, - = Up)", self.source)

    def test_return_to_start_replaces_repetitive_plan_summary(self) -> None:
        self.assertIn("Return-to-start check", self.page)
        self.assertIn("function renderRunPlanReturnStatus(model)", self.source)
        self.assertIn("Does not return to the tracked X/Z starting position", self.source)
        self.assertNotIn("What this plan does", self.page)

    def test_all_atomic_actions_have_preview_metadata(self) -> None:
        for action in (
            "move_x",
            "move_z",
            "move_xz_parallel",
            "rotation",
            "set_rpm",
            "stop_rpm",
            "wait",
            "echem",
            "rpm_echem",
            "rinse",
            "gamry_cell_on",
            "gamry_cell_off",
        ):
            with self.subTest(action=action):
                self.assertIn(f"{action}: {{ category:", self.source)

        self.assertIn('categoryLabel: "Unknown step"', self.source)
        self.assertIn("Unknown step type:", self.source)

    def test_duration_state_and_warning_rules_are_present(self) -> None:
        self.assertIn("function estimateProtocolDuration(protocolName)", self.source)
        self.assertIn("function estimateRunPlanStepDuration(step)", self.source)
        self.assertIn("wait_s_between_steps", self.source)
        self.assertIn("step.step_count", self.source)
        self.assertIn("planned relative moves only", self.source)

        expected_warnings = (
            "EChem step runs without active RPM",
            "RDE may start spinning before the electrode",
            "X movement may occur while Z is lowered",
            "Gamry cell may remain ON",
            "RDE may remain spinning at the end",
            "long wait over 5 minutes",
            "disabled and will be skipped",
        )
        for warning in expected_warnings:
            with self.subTest(warning=warning):
                self.assertIn(warning, self.source)

    def test_preview_reacts_to_builder_edits_and_protocol_refreshes(self) -> None:
        self.assertIn('recipeGroupsEl.addEventListener("input", scheduleRunPlanPreview)', self.source)
        self.assertIn('recipeGroupsEl.addEventListener("change", scheduleRunPlanPreview)', self.source)
        self.assertIn("new MutationObserver(scheduleRunPlanPreview)", self.source)
        self.assertIn("await refreshProtocolPreviewCache()", self.source)
        self.assertIn('automationRepetitionsInput.addEventListener("input"', self.source)

    def test_config_exposes_read_only_rinse_duration_for_preview(self) -> None:
        response = app.test_client().get("/api/config")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertGreater(float(payload["config"]["rinse_duration_s"]), 0)


if __name__ == "__main__":
    unittest.main()
