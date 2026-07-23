from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class IntegrationResult:
    time_s: List[float] = field(default_factory=list)
    values: List[float] = field(default_factory=list)
    cumulative_integral: List[float] = field(default_factory=list)
    source_point_count: int = 0
    integrated_interval_count: int = 0
    skipped_interval_count: int = 0
    integrated_duration_s: float = 0.0
    time_monotonic: bool = True
    warnings: List[str] = field(default_factory=list)

    @property
    def final_integral(self) -> float:
        return self.cumulative_integral[-1] if self.cumulative_integral else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "time_s": list(self.time_s),
            "values": list(self.values),
            "cumulative_integral": list(self.cumulative_integral),
            "source_point_count": int(self.source_point_count),
            "integrated_interval_count": int(self.integrated_interval_count),
            "skipped_interval_count": int(self.skipped_interval_count),
            "integrated_duration_s": float(self.integrated_duration_s),
            "time_monotonic": bool(self.time_monotonic),
            "warnings": list(self.warnings),
        }
