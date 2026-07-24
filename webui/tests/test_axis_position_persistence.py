from __future__ import annotations

import unittest
from pathlib import Path

from workflow.state import (
    disable_axis_position_persistence,
    enable_axis_position_persistence,
    get_axis_position,
    get_axis_position_confidence,
    mark_axis_positions_uncertain,
    reset_axis_positions,
    set_axis_position,
)


class AxisPositionPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        disable_axis_position_persistence()
        reset_axis_positions()

    def tearDown(self) -> None:
        disable_axis_position_persistence()
        reset_axis_positions()

    def test_positions_and_confidence_survive_reload(self) -> None:
        state_path = (
            Path(__file__).resolve().parents[1]
            / "output"
            / ".test-axis-position-state.json"
        )
        temp_path = state_path.with_suffix(".json.tmp")
        state_path.unlink(missing_ok=True)
        temp_path.unlink(missing_ok=True)

        try:
            enable_axis_position_persistence(state_path)

            set_axis_position("horizontal", 255000)
            set_axis_position("linear", 70000)
            mark_axis_positions_uncertain(("vertical",))

            self.assertTrue(state_path.is_file())
            self.assertFalse(state_path.with_suffix(".json.tmp").exists())

            disable_axis_position_persistence()
            reset_axis_positions()
            enable_axis_position_persistence(state_path)

            self.assertEqual(get_axis_position("horizontal"), 255000)
            self.assertEqual(get_axis_position("linear"), 70000)
            self.assertEqual(
                get_axis_position_confidence("vertical"),
                "uncertain",
            )
        finally:
            disable_axis_position_persistence()
            state_path.unlink(missing_ok=True)
            temp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
