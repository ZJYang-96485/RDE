"""Pure numerical integration with no hardware, Flask, or filesystem dependencies."""

from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence, Set, Tuple

from analysis.schemas import IntegrationResult


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _warning_messages(invalid: int, duplicate: int, backward: int, replay: int = 0) -> List[str]:
    warnings: List[str] = []
    if invalid:
        warnings.append(
            f"Skipped {invalid} interval(s) with a missing or non-finite endpoint."
        )
    if duplicate:
        warnings.append(
            f"Skipped {duplicate} interval(s) with duplicate timestamps (dt == 0)."
        )
    if backward:
        warnings.append(
            f"Skipped {backward} interval(s) with nonmonotonic timestamps (dt < 0)."
        )
    if replay:
        warnings.append(f"Ignored {replay} duplicate live point replay(s).")
    return warnings


class StreamingTrapezoidAccumulator:
    """Stateful composite-trapezoid accumulator for exactly one acquisition.

    ``deduplicate=True`` is intended for a live ToolkitPy stream, where a ring
    buffer reset can replay already emitted points. Batch analysis disables
    replay de-duplication so every adjacent source interval is audited.
    """

    def __init__(self, *, deduplicate: bool = True) -> None:
        self.deduplicate = bool(deduplicate)
        self._time_s: List[float] = []
        self._values: List[float] = []
        self._cumulative: List[float] = []
        self._source_point_count = 0
        self._integrated_interval_count = 0
        self._skipped_interval_count = 0
        self._integrated_duration_s = 0.0
        self._time_monotonic = True
        self._invalid_interval_count = 0
        self._duplicate_timestamp_count = 0
        self._backward_timestamp_count = 0
        self._duplicate_replay_count = 0
        self._previous_valid = False
        self._previous_time_s = 0.0
        self._previous_value = 0.0
        self._charge = 0.0
        self._seen_finite_points: Set[Tuple[float, float]] = set()
        self.last_point_accepted = False

    @property
    def cumulative_integral(self) -> float:
        return float(self._charge)

    @property
    def integrated_interval_count(self) -> int:
        return int(self._integrated_interval_count)

    @property
    def skipped_interval_count(self) -> int:
        return int(self._skipped_interval_count)

    @property
    def warnings(self) -> List[str]:
        return _warning_messages(
            self._invalid_interval_count,
            self._duplicate_timestamp_count,
            self._backward_timestamp_count,
            self._duplicate_replay_count,
        )

    def add_point(self, time_s: Any, value: Any) -> float:
        time_value = _finite(time_s)
        current_value = _finite(value)
        self.last_point_accepted = False

        if time_value is not None and current_value is not None and self.deduplicate:
            fingerprint = (time_value, current_value)
            if fingerprint in self._seen_finite_points:
                self._duplicate_replay_count += 1
                return self.cumulative_integral
            self._seen_finite_points.add(fingerprint)

        source_index = self._source_point_count
        self._source_point_count += 1
        current_valid = time_value is not None and current_value is not None

        if source_index > 0:
            if self._previous_valid and current_valid:
                assert time_value is not None and current_value is not None
                dt = time_value - self._previous_time_s
                if dt > 0:
                    increment = 0.5 * (self._previous_value + current_value) * dt
                    if math.isfinite(increment) and math.isfinite(self._charge + increment):
                        self._charge += increment
                        self._integrated_interval_count += 1
                        self._integrated_duration_s += dt
                    else:
                        self._skipped_interval_count += 1
                        self._invalid_interval_count += 1
                elif dt == 0:
                    self._skipped_interval_count += 1
                    self._duplicate_timestamp_count += 1
                else:
                    self._skipped_interval_count += 1
                    self._backward_timestamp_count += 1
                    self._time_monotonic = False
            else:
                self._skipped_interval_count += 1
                self._invalid_interval_count += 1

        if current_valid:
            assert time_value is not None and current_value is not None
            self._time_s.append(time_value)
            self._values.append(current_value)
            self._cumulative.append(float(self._charge))
            self.last_point_accepted = True

        self._previous_valid = current_valid
        if current_valid:
            self._previous_time_s = float(time_value)
            self._previous_value = float(current_value)
        return self.cumulative_integral

    def result(self) -> IntegrationResult:
        return IntegrationResult(
            time_s=list(self._time_s),
            values=list(self._values),
            cumulative_integral=list(self._cumulative),
            source_point_count=self._source_point_count,
            integrated_interval_count=self._integrated_interval_count,
            skipped_interval_count=self._skipped_interval_count,
            integrated_duration_s=self._integrated_duration_s,
            time_monotonic=self._time_monotonic,
            warnings=self.warnings,
        )


def cumulative_trapezoid(
    time_s: Sequence[float],
    values: Sequence[float],
) -> IntegrationResult:
    """Integrate adjacent source points using the composite trapezoidal rule."""

    times = list(time_s)
    samples = list(values)
    accumulator = StreamingTrapezoidAccumulator(deduplicate=False)
    source_count = max(len(times), len(samples))
    for index in range(source_count):
        accumulator.add_point(
            times[index] if index < len(times) else None,
            samples[index] if index < len(samples) else None,
        )
    return accumulator.result()
