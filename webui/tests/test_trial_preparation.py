from __future__ import annotations

import unittest
from unittest.mock import patch

from gamry_worker.ir_compensation import apply_trial_settings, disable_ir_compensation
from gamry_worker.trial_preparation import CriticalHardwareError, default_trial_metadata, determine_ru

try:
    import toolkitpy as tkp
    from gamry_worker.real_trial_preparation import _initialize_for_ru, _measure_ru_once
except (ImportError, OSError):
    tkp = None
    _initialize_for_ru = None
    _measure_ru_once = None


SETTINGS = {
    "ru_retry_count": 3,
    "ru_repeatability_limit": 0.05,
    "ru_min_ohm": 1.0,
    "ru_max_ohm": 1000.0,
    "compensation_fraction": 0.8,
}


class FakePstat:
    def __init__(self) -> None:
        self.enabled = False
        self.resistance = None
        self.range_mode = None
        self.range_value = None
        self.compensation_calls = []

    def set_pos_feed_enable(self, value):
        self.enabled = bool(value)
        self.compensation_calls.append(("enable", bool(value)))

    def set_pos_feed_resistance(self, value):
        self.resistance = float(value)
        self.compensation_calls.append(("resistance", float(value)))

    def pos_feed_enable(self):
        return self.enabled

    def pos_feed_resistance(self):
        return self.resistance

    def test_ie_range(self, value):
        return f"range:{value}"

    def set_ie_range(self, value):
        self.range_value = value

    def set_ie_range_mode(self, value):
        self.range_mode = value


class UnsupportedPositiveFeedbackPstat(FakePstat):
    def set_pos_feed_enable(self, value):
        self.enabled = False
        self.compensation_calls.append(("enable", bool(value)))

    def set_pos_feed_resistance(self, value):
        self.resistance = 0.0
        self.compensation_calls.append(("resistance", float(value)))


class RecordingPstat:
    def __init__(self) -> None:
        self.calls = []
        self.cell_state = None

    def __getattr__(self, name):
        if name.startswith("set_"):
            def record(*args):
                self.calls.append((name, args))
                if name == "set_cell":
                    self.cell_state = args[0]
                return args[0] if args else None

            return record
        raise AttributeError(name)

    def cell(self):
        return self.cell_state

    def freq_limit_lower(self):
        return 0.01

    def freq_limit_upper(self):
        return 1_000_000.0

    def measure_i(self):
        return 0.0

    def measure_temp(self):
        return 25.0

    def test_ie_range(self, _value):
        return 3

    def test_vch_range(self, required_v):
        self.calls.append(("test_vch_range", (required_v,)))
        return 0 if required_v < 0.03 else 1


class FakeReadZ:
    def __init__(self, pstat) -> None:
        self.pstat = pstat
        self.passes = 10
        self.zmod = 42.0
        self.gain = 1.0

    def __getattr__(self, name):
        if name.startswith("set_"):
            return lambda *_args: None
        raise AttributeError(name)

    def measure(self, *_args):
        assert ("set_sig_gen_filter", (10,)) in self.pstat.calls
        assert self.pstat.test_vch_range(0.0071) == 1
        return True


class FakeZCurve:
    def __init__(self, _count) -> None:
        pass

    def add_point(self, _readz, _temperature):
        pass

    def acq_data(self):
        return [["fake point"]]


class TrialPreparationTests(unittest.TestCase):
    def test_default_compensation_uses_full_validated_ru(self) -> None:
        metadata = default_trial_metadata()
        self.assertEqual(metadata["compensation_fraction"], 1.0)

    @unittest.skipUnless(tkp is not None, "requires the 32-bit Gamry ToolkitPy runtime")
    def test_ru_initialization_matches_vendor_control_settings(self) -> None:
        pstat = RecordingPstat()
        _initialize_for_ru(pstat)
        self.assertIn(("set_ca_speed", (tkp.CASPEED_MEDFAST,)), pstat.calls)
        self.assertIn(("set_sense_speed_mode", (True,)), pstat.calls)

    @unittest.skipUnless(tkp is not None, "requires the 32-bit Gamry ToolkitPy runtime")
    def test_ru_measurement_applies_vendor_signal_generator_filter_fix(self) -> None:
        pstat = RecordingPstat()
        with patch("gamry_worker.real_trial_preparation.tkp.pstat_is_valid", return_value=True):
            with patch("gamry_worker.real_trial_preparation.tkp.ReadZ", FakeReadZ):
                with patch("gamry_worker.real_trial_preparation.tkp.ZCurve", FakeZCurve):
                    with patch(
                        "gamry_worker.real_trial_preparation.normalize_eis_point",
                        return_value={"zreal_ohm": 18.5},
                    ):
                        with patch("gamry_worker.real_trial_preparation.time.sleep"):
                            result = _measure_ru_once(pstat, {}, 0.01)
        self.assertEqual(result, 18.5)
        self.assertIn(("set_sig_gen_filter", (10,)), pstat.calls)
        self.assertEqual(pstat.test_vch_range(0.0071), 0)

    def test_ru_failure_can_continue_without_ir_compensation(self) -> None:
        events = []
        result = determine_ru(
            lambda _attempt: None,
            {
                "ru_retry_count": 3,
                "continue_without_ir_on_ru_failure": True,
            },
            emit_event=lambda event_type, **fields: events.append((event_type, fields)),
        )

        self.assertFalse(result["ru_validation_passed"])
        self.assertTrue(result["measurement_without_ir_compensation"])
        self.assertEqual(result["trial_status"], "ready_without_ir_compensation")
        self.assertIsNone(result["skip_reason"])
        self.assertIn("Unable to obtain", result["ru_failure_reason"])
        self.assertNotIn("trial_skipped", [event[0] for event in events])

    def test_valid_ru_on_first_two_measurements(self) -> None:
        values = iter([18.4, 18.8])
        result = determine_ru(lambda _attempt: next(values), SETTINGS)
        self.assertTrue(result["ru_validation_passed"])
        self.assertAlmostEqual(result["ru_selected_ohm"], 18.6)
        self.assertAlmostEqual(result["ru_applied_ohm"], 14.88)
        self.assertEqual(len(result["ru_attempts_ohm"]), 2)

    def test_third_measurement_uses_repeatable_median(self) -> None:
        values = iter([10.0, 30.0, 10.2])
        result = determine_ru(lambda _attempt: next(values), SETTINGS)
        self.assertTrue(result["ru_validation_passed"])
        self.assertAlmostEqual(result["ru_selected_ohm"], 10.2)
        self.assertEqual(len(result["ru_attempts_ohm"]), 3)

    def test_out_of_range_and_null_bypass_after_maximum_attempts(self) -> None:
        for values in ([0.1, 0.2, 0.3], [None, None, None]):
            with self.subTest(values=values):
                iterator = iter(values)
                result = determine_ru(lambda _attempt: next(iterator), SETTINGS)
                self.assertFalse(result["ru_validation_passed"])
                self.assertEqual(result["trial_status"], "skipped")
                self.assertIn("Unable to obtain", result["skip_reason"])

    def test_noncritical_measurement_exception_retries_then_bypasses(self) -> None:
        result = determine_ru(lambda _attempt: (_ for _ in ()).throw(ValueError("bad point")), SETTINGS)
        self.assertEqual(result["trial_status"], "skipped")
        self.assertEqual(len(result["ru_attempts_ohm"]), 3)

    def test_critical_hardware_error_aborts(self) -> None:
        with self.assertRaises(CriticalHardwareError):
            determine_ru(
                lambda _attempt: (_ for _ in ()).throw(RuntimeError("potentiostat communication loss")),
                SETTINGS,
            )

    def test_previous_ru_is_never_reused(self) -> None:
        first_values = iter([10.0, 10.1])
        first = determine_ru(lambda _attempt: next(first_values), SETTINGS)
        second_values = iter([20.0, 20.2])
        second = determine_ru(lambda _attempt: next(second_values), SETTINGS)
        self.assertNotEqual(first["ru_attempts_ohm"], second["ru_attempts_ohm"])
        self.assertAlmostEqual(second["ru_selected_ohm"], 20.1)

    def test_eis_never_enables_positive_feedback(self) -> None:
        pstat = FakePstat()
        result = apply_trial_settings(
            pstat,
            {
                "technique": "eis",
                "_trial_ru_validation_passed": True,
                "_trial_ru_applied_ohm": 15.0,
                "_trial_fixed_current_range_a": 0.003,
            },
        )
        self.assertFalse(result["ir_compensation_enabled"])
        self.assertFalse(pstat.enabled)

    def test_compensation_is_disabled_for_success_failure_and_exception_cleanup(self) -> None:
        for outcome in ("success", "failure", "exception"):
            with self.subTest(outcome=outcome):
                pstat = FakePstat()
                apply_trial_settings(
                    pstat,
                    {
                        "technique": "cv",
                        "_trial_ru_validation_passed": True,
                        "_trial_ru_applied_ohm": 15.0,
                        "_trial_fixed_current_range_a": 0.003,
                    },
                )
                self.assertTrue(pstat.enabled)
                self.assertEqual(
                    pstat.compensation_calls[-2:],
                    [("enable", True), ("resistance", 15.0)],
                )
                try:
                    if outcome == "exception":
                        raise RuntimeError("measurement failed")
                except RuntimeError:
                    pass
                finally:
                    disable_ir_compensation(pstat)
                self.assertFalse(pstat.enabled)

    def test_unsupported_positive_feedback_is_reported_and_measurement_can_continue(self) -> None:
        pstat = UnsupportedPositiveFeedbackPstat()
        result = apply_trial_settings(
            pstat,
            {
                "technique": "cv",
                "_trial_ru_validation_passed": True,
                "_trial_ru_applied_ohm": 15.0,
                "_trial_fixed_current_range_a": 0.003,
            },
        )
        self.assertTrue(result["ir_compensation_requested"])
        self.assertFalse(result["ir_compensation_enabled"])
        self.assertIn("not supported", result["ir_compensation_reason"])
        self.assertFalse(pstat.enabled)


if __name__ == "__main__":
    unittest.main()
