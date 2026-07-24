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
        self.assertIn('echem: \'<path d="M3 18h18', self.page)
        self.assertNotIn("cdnjs.cloudflare.com", self.page)

    def test_preview_is_after_the_builder_and_never_parallel(self) -> None:
        builder_position = self.page.index('id="runPlanBuilderPanel"')
        preview_position = self.page.index('id="runPlanPreviewPanel"')
        self.assertLess(builder_position, preview_position)
        self.assertIn(".run-plan-layout {\n        display: block;", self.source)
        self.assertIn("position: static;", self.source)
        self.assertNotIn("grid-template-columns: minmax(0, 1.2fr)", self.source)

    def test_builder_and_preview_share_one_icon_tab_workspace(self) -> None:
        self.assertIn('id="runPlanBuilderTab"', self.page)
        self.assertIn('id="runPlanPreviewTab"', self.page)
        self.assertIn('id="runPlanBuilderPanel"', self.page)
        self.assertIn('id="runPlanPreviewPanel"', self.page)
        self.assertIn('id="runPlanPreviewPanel" class="run-plan-preview-panel run-plan-view-panel" role="tabpanel" aria-labelledby="runPlanPreviewTab" hidden', self.page)
        self.assertIn("builder: '<path", self.source)
        self.assertIn('runPlanIcon("builder", "plan", true)', self.source)
        self.assertIn('runPlanIcon("plan", "plan", true)', self.source)
        self.assertIn("function setRunPlanWorkspaceView(view, focusTab = false)", self.source)
        self.assertIn("runPlanBuilderPanelEl.hidden = showPreview", self.source)
        self.assertIn("runPlanPreviewPanelEl.hidden = !showPreview", self.source)
        self.assertIn('localStorage.setItem("rdeRunPlanWorkspaceView"', self.source)

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

    def test_x_direction_is_explicit_and_matches_hardware_convention(self) -> None:
        self.assertIn("motionXLeft:", self.source)
        self.assertIn("motionXRight:", self.source)
        self.assertIn('if (steps > 0) return "Left"', self.source)
        self.assertIn('if (steps < 0) return "Right"', self.source)
        self.assertIn("X signed relative steps (+ = Left, - = Right)", self.source)

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
            "rinse",
            "set_rpm",
            "stop_rpm",
            "wait",
            "echem",
            "rpm_echem",
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

    def test_new_plan_is_next_to_saved_plans_and_cell_on_defaults_immediate(self) -> None:
        saved_label = self.page.index('<label for="recipeSelect">Saved Run Plans</label>')
        selector = self.page.index('id="recipeSelect"', saved_label)
        new_button = self.page.index('id="newBlankRecipeBtn"', selector)
        builder = self.page.index('id="runPlanBuilderPanel"')
        self.assertLess(new_button, builder)
        self.assertIn('? String(values.duration_s ?? 0)', self.source)
        self.assertIn('Default 0 turns the cell ON and immediately continues', self.source)

    def test_preview_reacts_to_builder_edits_and_protocol_refreshes(self) -> None:
        self.assertIn('recipeGroupsEl.addEventListener("input", scheduleRunPlanPreview)', self.source)
        self.assertIn('recipeGroupsEl.addEventListener("change", scheduleRunPlanPreview)', self.source)
        self.assertIn("new MutationObserver(scheduleRunPlanPreview)", self.source)
        self.assertIn("await refreshProtocolPreviewCache()", self.source)
        self.assertIn('automationRepetitionsInput.addEventListener("input"', self.source)

    def test_packaged_concurrent_rinse_action_is_present(self) -> None:
        self.assertIn(
            '<option value="rinse">Packaged Concurrent Rinse</option>',
            self.page,
        )
        self.assertIn('rinse: { category:', self.source)
        self.assertIn("atomic-rinse-cycles", self.source)
        self.assertIn("atomic-rinse-x-radius", self.source)
        self.assertIn("atomic-rinse-z-radius", self.source)
        self.assertIn("atomic-rinse-arm-worker-amplitude", self.source)
        self.assertIn("atomic-rinse-rpm", self.source)
        self.assertIn("atomic-rinse-immersed-confirmed", self.source)
        self.assertIn("continuous arm worker", self.source)
        self.assertIn("RPM starts once", self.source)

        response = app.test_client().get("/api/config")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["config"]["rinse_rpm_max"], 300)

    def test_arm_only_run_plan_action_is_replaced_by_rinse(self) -> None:
        self.assertNotIn(
            '<option value="rinse_arm_oscillation">',
            self.page,
        )
        self.assertNotIn("rinse_arm_oscillation: { category:", self.source)
        self.assertNotIn("atomic-rinse-arm-enabled", self.source)
        self.assertNotIn("atomic-rinse-arm-cycles", self.source)

        payload = app.test_client().get("/api/config").get_json()["config"]
        self.assertFalse(payload["rotation_arm"]["rinse_oscillation"]["enabled"])
        self.assertEqual(payload["rotation_arm"]["degrees_per_step"], 0.225)
        self.assertEqual(payload["rotation_arm"]["max_relative_steps"], 44)


if __name__ == "__main__":
    unittest.main()
