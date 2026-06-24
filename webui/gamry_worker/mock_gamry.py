from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class MockGamryError(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def sample_times(duration_s: float, sample_period_s: float, max_points: int = 2000) -> list[float]:
    duration_s = max(0.0, float(duration_s))
    sample_period_s = max(1e-6, float(sample_period_s))

    n_points = int(duration_s / sample_period_s) + 1

    if n_points > max_points:
        n_points = max_points
        sample_period_s = duration_s / max(1, n_points - 1)

    return [i * sample_period_s for i in range(n_points)]


def write_text(path: str | Path, text: str) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")


def header(step: dict[str, Any], technique: str) -> list[str]:
    return [
        "MOCK_GAMRY_DATA",
        f"CREATED_AT\t{now_iso()}",
        f"TECHNIQUE\t{technique}",
        f"STEP_NAME\t{step.get('name', '')}",
        "PARAMETERS_JSON",
        json.dumps(step, indent=2),
        "END_PARAMETERS_JSON",
        ""
    ]


def run_ocp(step: dict[str, Any], output_path: str | Path) -> dict[str, Any]:
    duration_s = as_float(step.get("duration_s"), 60)
    sample_period_s = as_float(step.get("sample_period_s"), 0.5)
    times = sample_times(duration_s, sample_period_s)

    lines = header(step, "ocp")
    lines.append("time_s\tpotential_v")

    for t in times:
        potential = 0.02 + 0.004 * math.exp(-t / max(duration_s, 1)) + 0.0005 * math.sin(t / 8)
        lines.append(f"{t:.6f}\t{potential:.9f}")

    write_text(output_path, "\n".join(lines) + "\n")

    return {
        "ok": True,
        "technique": "ocp",
        "output_path": str(output_path),
        "points": len(times)
    }


def run_ca(step: dict[str, Any], output_path: str | Path, voltage_v: float | None = None) -> dict[str, Any]:
    voltage = as_float(step.get("voltage_v"), 0.0) if voltage_v is None else float(voltage_v)
    duration_s = as_float(step.get("duration_s", step.get("step_time_s")), 300)
    sample_period_s = as_float(step.get("sample_period_s"), 1)
    times = sample_times(duration_s, sample_period_s)

    lines = header({**step, "applied_voltage_v": voltage}, "ca")
    lines.append("time_s\tapplied_voltage_v\tcurrent_a")

    base_current = -1e-5 * abs(voltage) - 2e-6

    for t in times:
        decay = math.exp(-t / max(duration_s / 4, 1))
        current = base_current * (1 + 0.35 * decay) + 1e-7 * math.sin(t / 12)
        lines.append(f"{t:.6f}\t{voltage:.9f}\t{current:.12e}")

    write_text(output_path, "\n".join(lines) + "\n")

    return {
        "ok": True,
        "technique": "ca",
        "output_path": str(output_path),
        "voltage_v": voltage,
        "points": len(times)
    }


def run_lsv(step: dict[str, Any], output_path: str | Path) -> dict[str, Any]:
    start_v = as_float(step.get("start_voltage_v"), 0.2)
    end_v = as_float(step.get("end_voltage_v"), -0.8)
    scan_rate_v_s = as_float(step.get("scan_rate_v_s"), 0.01)
    sample_period_s = as_float(step.get("sample_period_s"), 0.1)

    duration_s = abs(end_v - start_v) / max(scan_rate_v_s, 1e-9)
    times = sample_times(duration_s, sample_period_s)
    lines = header(step, "lsv")
    lines.append("time_s\tpotential_v\tcurrent_a")

    for t in times:
        fraction = 0 if duration_s == 0 else t / duration_s
        potential = start_v + (end_v - start_v) * fraction
        current = -2e-6 - 2e-5 / (1 + math.exp((potential + 0.35) / 0.08))
        lines.append(f"{t:.6f}\t{potential:.9f}\t{current:.12e}")

    write_text(output_path, "\n".join(lines) + "\n")

    return {
        "ok": True,
        "technique": "lsv",
        "output_path": str(output_path),
        "points": len(times)
    }


def run_cv(step: dict[str, Any], output_path: str | Path) -> dict[str, Any]:
    initial_v = as_float(step.get("initial_voltage_v"), 0)
    first_v = as_float(step.get("first_vertex_v"), 1)
    second_v = as_float(step.get("second_vertex_v"), -1)
    final_v = as_float(step.get("final_voltage_v"), initial_v)
    scan_rate_v_s = as_float(step.get("scan_rate_v_s"), 0.05)
    sample_period_s = as_float(step.get("sample_period_s"), 0.01)
    cycles = max(1, as_int(step.get("cycles"), 1))

    voltage_points = []

    for _ in range(cycles):
        voltage_points.extend([initial_v, first_v, second_v, final_v])

    rows = []
    current_time = 0.0

    for start, end in zip(voltage_points[:-1], voltage_points[1:]):
        segment_duration = abs(end - start) / max(scan_rate_v_s, 1e-9)
        times = sample_times(segment_duration, sample_period_s, max_points=1000)

        for t in times:
            fraction = 0 if segment_duration == 0 else t / segment_duration
            potential = start + (end - start) * fraction
            current = 8e-6 * math.tanh((potential - 0.1) / 0.18) + 2e-6 * math.sin(3 * potential)
            rows.append((current_time + t, potential, current))

        current_time += segment_duration

    lines = header(step, "cv")
    lines.append("time_s\tpotential_v\tcurrent_a")

    for t, potential, current in rows:
        lines.append(f"{t:.6f}\t{potential:.9f}\t{current:.12e}")

    write_text(output_path, "\n".join(lines) + "\n")

    return {
        "ok": True,
        "technique": "cv",
        "output_path": str(output_path),
        "points": len(rows)
    }


def logspace_descending(start: float, stop: float, points_per_decade: int) -> list[float]:
    start = max(start, 1e-12)
    stop = max(stop, 1e-12)
    points_per_decade = max(1, int(points_per_decade))

    log_start = math.log10(start)
    log_stop = math.log10(stop)
    total_points = int(abs(log_start - log_stop) * points_per_decade) + 1

    if total_points < 2:
        total_points = 2

    return [
        10 ** (log_start + (log_stop - log_start) * i / (total_points - 1))
        for i in range(total_points)
    ]


def run_eis(step: dict[str, Any], output_path: str | Path) -> dict[str, Any]:
    initial_frequency_hz = as_float(step.get("initial_frequency_hz"), 100000)
    final_frequency_hz = as_float(step.get("final_frequency_hz"), 0.1)
    points_per_decade = as_int(step.get("points_per_decade"), 10)

    frequencies = logspace_descending(initial_frequency_hz, final_frequency_hz, points_per_decade)

    rs = 20.0
    rct = as_float(step.get("estimated_z_ohm"), 100)
    cdl = 2e-5

    lines = header(step, "eis")
    lines.append("frequency_hz\tzreal_ohm\tzimag_ohm\tzmod_ohm\tphase_deg")

    for freq in frequencies:
        omega = 2 * math.pi * freq
        denom = 1 + (omega * rct * cdl) ** 2
        zreal = rs + rct / denom
        zimag = -(omega * rct * rct * cdl) / denom
        zmod = math.sqrt(zreal * zreal + zimag * zimag)
        phase = math.degrees(math.atan2(zimag, zreal))
        lines.append(f"{freq:.9e}\t{zreal:.9f}\t{zimag:.9f}\t{zmod:.9f}\t{phase:.9f}")

    write_text(output_path, "\n".join(lines) + "\n")

    return {
        "ok": True,
        "technique": "eis",
        "output_path": str(output_path),
        "points": len(frequencies)
    }


def run_ca_staircase(step: dict[str, Any], outputs: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    start_voltage_v = as_float(step.get("start_voltage_v"), -0.1)
    step_voltage_v = as_float(step.get("step_voltage_v"), -0.1)

    for output in outputs:
        staircase_index = as_int(output.get("index"), 1)
        voltage = output.get("voltage_v")

        if voltage is None:
            voltage = start_voltage_v + step_voltage_v * (staircase_index - 1)

        ca_step = {
            **step,
            "technique": "ca",
            "voltage_v": voltage,
            "duration_s": step.get("step_time_s", 300)
        }

        results.append(run_ca(ca_step, output["path"], voltage_v=float(voltage)))

    return {
        "ok": True,
        "technique": "ca_staircase",
        "outputs": results
    }


def run_step(step: dict[str, Any], outputs: list[dict[str, Any]]) -> dict[str, Any]:
    if not outputs:
        raise MockGamryError("outputs cannot be empty.")

    technique = str(step.get("technique", "")).lower().strip()

    if technique == "ocp":
        return run_ocp(step, outputs[0]["path"])

    if technique == "ca":
        return run_ca(step, outputs[0]["path"])

    if technique == "ca_staircase":
        return run_ca_staircase(step, outputs)

    if technique == "cv":
        return run_cv(step, outputs[0]["path"])

    if technique == "lsv":
        return run_lsv(step, outputs[0]["path"])

    if technique == "eis":
        return run_eis(step, outputs[0]["path"])

    raise MockGamryError(f"unsupported mock technique: {technique}")


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    step = job.get("step")
    outputs = job.get("outputs")

    if not isinstance(step, dict):
        raise MockGamryError("job.step must be an object.")

    if not isinstance(outputs, list):
        raise MockGamryError("job.outputs must be a list.")

    delay_s = as_float(job.get("mock_delay_s"), 0.2)

    if delay_s > 0:
        time.sleep(min(delay_s, 5))

    result = run_step(step, outputs)

    return {
        "ok": True,
        "mode": "mock",
        "created_at": now_iso(),
        "result": result
    }