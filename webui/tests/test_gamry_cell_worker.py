from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from gamry_worker import cell_control


class FakeCellState:
    def __init__(self, name: str) -> None:
        self.name = name


class FakePstat:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[bool] = []
        self.current = False

    def open(self) -> None:
        pass

    def set_cell(self, enabled: bool) -> None:
        self.calls.append(enabled)
        self.current = enabled

    def cell(self) -> FakeCellState:
        return FakeCellState("CELL_ON" if self.current else "CELL_OFF")


class FakeToolkit:
    def __init__(self) -> None:
        self.pstat = None
        self.closed = False

    def toolkitpy_init(self, _name: str) -> None:
        pass

    def enum_sections(self) -> list[str]:
        return ["IFC1010-test"]

    def Pstat(self, name: str) -> FakePstat:
        self.pstat = FakePstat(name)
        return self.pstat

    def toolkitpy_close(self) -> None:
        self.closed = True


class GamryCellWorkerTests(unittest.TestCase):
    def test_timed_on_forces_off_and_reports_actual_readback(self) -> None:
        fake = FakeToolkit()
        module = SimpleNamespace(
            toolkitpy_init=fake.toolkitpy_init,
            enum_sections=fake.enum_sections,
            Pstat=fake.Pstat,
            toolkitpy_close=fake.toolkitpy_close,
        )

        with patch.dict(sys.modules, {"toolkitpy": module}):
            result = cell_control.run_command("on", 0.001, None)

        self.assertEqual(result["final_state"], "off")
        self.assertEqual(result["actual_state"], "off")
        self.assertEqual(fake.pstat.calls, [True, False, False])
        self.assertTrue(fake.closed)

    def test_status_uses_real_cell_method(self) -> None:
        fake = FakeToolkit()
        module = SimpleNamespace(
            toolkitpy_init=fake.toolkitpy_init,
            enum_sections=fake.enum_sections,
            Pstat=fake.Pstat,
            toolkitpy_close=fake.toolkitpy_close,
        )

        with patch.dict(sys.modules, {"toolkitpy": module}):
            result = cell_control.run_command("status", None, "IFC1010-test")

        self.assertEqual(result["actual_state"], "off")
        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
