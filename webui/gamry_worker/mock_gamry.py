from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from gamry_worker.live_adapters import (
        normalize_ca_acq_rows,
        normalize_cc_charge_acq_rows,
        normalize_cc_discharge_acq_rows,
        normalize_cp_acq_rows,
        normalize_cv_acq_rows,
        normalize_eis_point,
        normalize_geis_point,
        normalize_lsv_acq_rows,
        normalize_ocp_acq_rows,
    )
    from gamry_worker.live_writer import append_live_point
except ModuleNotFoundError:
    from live_adapters import (
        normalize_ca_acq_rows,
        normalize_cc_charge_acq_rows,
        normalize_cc_discharge_acq_rows,
        normalize_cp_acq_rows,
        normalize_cv_acq_rows,
        normalize_eis_point,
        normalize_geis_point,
        normalize_lsv_acq_rows,
        normalize_ocp_acq_rows,
    )
    from live_writer import append_live_point


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
        "",
    ]


class LiveEmitter:
    """Emit normalized mock points and scale only the wall-clock delay."""

    def __init__(self, live_dir: str | Path | None, mock_time_scale: float = 0.05) -> None:
        self.live_dir = Path(live_dir) if live_dir else None
        self.mock_time_scale = max(0.0, float(mock_time_scale))

    def emit(self, technique: str, point: dict[str, Any]) -> None:
        if self.live_dir is None:
            return

        normalizers: dict[str, Callable[[Any], dict[str, Any]]] = {
            "ocp": normalize_ocp_acq_rows,
            "ca": normalize_ca_acq_rows,
            "cp": normalize_cp_acq_rows,
            "cc_charge": normalize_cc_charge_acq_rows,
            "cc_discharge": normalize_cc_discharge_acq_rows,
            "cv": normalize_cv_acq_rows,
            "lsv": normalize_lsv_acq_rows,
            "eis": normalize_eis_point,
            "geis": normalize_geis_point,
        }
        normalized = normalizers[technique](point)
        append_live_point(self.live_dir, normalized)

    def wait(self, acquisition_seconds: float = 0.1) -> None:
        delay = max(0.0, float(acquisition_seconds)) * self.mock_time_scale
        if delay > 0:
            time.sleep(min(delay, 0.25))


def run_ocp(
    step: dict[str, Any],
    output_path: str | Path,
    emitter: LiveEmitter | None = None,
) -> dict[str, Any]:
    duration_s = as_float(step.get("duration_s"), 60)
    sample_period_s = as_float(step.get("sample_period_s"), 0.5)
    times = sample_times(duration_s, sample_period_s)

    lines = header(step, "ocp")
    lines.append("time_s\tpotential_v")

    for t in times:
        potential = 0.02 + 0.004 * math.exp(-t / max(duration_s, 1)) + 0.0005 * math.sin(t / 8)
        lines.append(f"{t:.6f}\t{potential:.9f}")
        if emitter:
            emitter.emit("ocp", {"t_s": t, "e_v": potential})
            emitter.wait(sample_period_s)

    write_text(output_path, "\n".join(lines) + "\n")

    return {
        "ok": True,
        "technique": "ocp",
        "output_path": str(output_path),
        "points": len(times),
    }


def run_ca(
    step: dict[str, Any],
    output_path: str | Path,
    voltage_v: float | None = None,
    emitter: LiveEmitter | None = None,
    time_offset_s: float = 0.0,
) -> dict[str, Any]:
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
        if emitter:
            emitter.emit(
                "ca",
                {
                    "t_s": time_offset_s + t,
                    "e_v": voltage,
                    "i_a": current,
                },
            )
            emitter.wait(sample_period_s)

    write_text(output_path, "\n".join(lines) + "\n")

    return {
        "ok": True,
        "technique": "ca",
        "output_path": str(output_path),
        "voltage_v": voltage,
        "points": len(times),
        "duration_s": duration_s,
    }


def run_cp(
    step: dict[str, Any],
    output_path: str | Path,
    emitter: LiveEmitter | None = None,
) -> dict[str, Any]:
    current_a = as_float(step.get("current_a"), 1e-5)
    duration_s = as_float(step.get("duration_s"), 60)
    sample_period_s = as_float(step.get("sample_period_s"), 0.5)
    voltage_min_v = as_float(step.get("voltage_limit_low_v", step.get("voltage_min_v")), -10)
    voltage_max_v = as_float(step.get("voltage_limit_high_v", step.get("voltage_max_v")), 10)
    expected_max_current_a = as_float(step.get("expected_max_current_a"), abs(current_a))
    if current_a == 0:
        raise MockGamryError("CP current_a must not be zero.")
    if voltage_min_v >= voltage_max_v:
        raise MockGamryError("CP voltage_limit_low_v must be lower than voltage_limit_high_v.")
    if expected_max_current_a <= 0 or expected_max_current_a < abs(current_a):
        raise MockGamryError("CP expected_max_current_a must cover the applied current.")
    times = sample_times(duration_s, sample_period_s)
    lines = header(step, "cp")
    lines.append("Pt\tT\tVf\tIm\tQ_Ah")
    emitted = 0
    final_voltage_v = None
    elapsed_s = 0.0
    capacity_ah = 0.0
    stop_reason = "duration_complete"
    stop_detail = None

    for point, t in enumerate(times):
        polarity = 1.0 if current_a >= 0 else -1.0
        potential = 0.05 + polarity * (0.02 + 0.03 * (1 - math.exp(-t / max(duration_s / 4, 1e-9))))
        capacity_ah = abs(current_a) * t / 3600.0
        lines.append(f"{point}\t{t:.6f}\t{potential:.9f}\t{current_a:.12e}\t{capacity_ah:.12e}")
        emitted += 1
        elapsed_s = t
        final_voltage_v = potential
        if emitter:
            emitter.emit("cp", {"t_s": t, "e_v": potential, "i_a": current_a})
            emitter.wait(sample_period_s)
        if potential <= voltage_min_v:
            stop_reason = "voltage_cutoff"
            stop_detail = "voltage_limit_low_v"
            break
        if potential >= voltage_max_v:
            stop_reason = "voltage_cutoff"
            stop_detail = "voltage_limit_high_v"
            break

    write_text(output_path, "\n".join(lines) + "\n")
    return {
        "ok": True,
        "technique": "cp",
        "output_path": str(output_path),
        "current_a": current_a,
        "expected_max_current_a": expected_max_current_a,
        "elapsed_s": elapsed_s,
        "final_voltage_v": final_voltage_v,
        "capacity_ah": capacity_ah,
        "stop_reason": stop_reason,
        "stop_detail": stop_detail,
        "points": emitted,
    }


def run_cc(
    step: dict[str, Any],
    output_path: str | Path,
    technique: str,
    emitter: LiveEmitter | None = None,
) -> dict[str, Any]:
    current_a = as_float(step.get("current_a"), 1e-5)
    duration_s = as_float(step.get("duration_s"), 60)
    sample_period_s = as_float(step.get("sample_period_s"), 1)
    cutoff_v = as_float(step.get("voltage_cutoff_v"), 4.2 if technique == "cc_charge" else 3.0)
    capacity_cutoff_ah = step.get("capacity_cutoff_ah")
    capacity_limit = None if capacity_cutoff_ah in {None, ""} else as_float(capacity_cutoff_ah)
    expected_max_current_a = as_float(step.get("expected_max_current_a"), current_a)
    if current_a <= 0:
        raise MockGamryError(f"{technique} current_a is a positive magnitude; direction is selected by the technique.")
    if expected_max_current_a <= 0 or expected_max_current_a < current_a:
        raise MockGamryError(f"{technique} expected_max_current_a must be at least current_a.")
    times = sample_times(duration_s, sample_period_s)
    lines = header(step, technique)
    lines.append("Pt\tT\tVf\tIm\tQ_Ah")
    emitted = 0
    final_voltage_v = None
    elapsed_s = 0.0
    capacity_ah = 0.0
    stop_reason = "duration_complete"

    for point, t in enumerate(times):
        fraction = 0 if duration_s <= 0 else min(1.0, t / duration_s)
        if technique == "cc_charge":
            potential = cutoff_v - 0.4 * (1 - fraction)
            measured_current = current_a
            reached_voltage = potential >= cutoff_v
        else:
            potential = cutoff_v + 0.4 * (1 - fraction)
            measured_current = -current_a
            reached_voltage = potential <= cutoff_v
        capacity_ah = current_a * t / 3600.0
        reached_capacity = capacity_limit is not None and capacity_ah >= capacity_limit
        lines.append(f"{point}\t{t:.6f}\t{potential:.9f}\t{measured_current:.12e}\t{capacity_ah:.12e}")
        emitted += 1
        elapsed_s = t
        final_voltage_v = potential
        if emitter:
            emitter.emit(technique, {"t_s": t, "e_v": potential, "i_a": measured_current})
            emitter.wait(sample_period_s)
        if reached_capacity:
            stop_reason = "capacity_cutoff"
            break
        if reached_voltage:
            stop_reason = "voltage_cutoff"
            break

    write_text(output_path, "\n".join(lines) + "\n")
    return {
        "ok": True,
        "technique": technique,
        "output_path": str(output_path),
        "current_a": current_a,
        "expected_max_current_a": expected_max_current_a,
        "elapsed_s": elapsed_s,
        "final_voltage_v": final_voltage_v,
        "capacity_ah": capacity_ah,
        "stop_reason": stop_reason,
        "points": emitted,
    }


def run_lsv(
    step: dict[str, Any],
    output_path: str | Path,
    emitter: LiveEmitter | None = None,
) -> dict[str, Any]:
    start_v = as_float(step.get("start_voltage_v", step.get("initial_voltage_v")), 0.2)
    end_v = as_float(step.get("end_voltage_v", step.get("final_voltage_v")), -0.8)
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
        if emitter:
            emitter.emit("lsv", {"t_s": t, "e_v": potential, "i_a": current})
            emitter.wait(sample_period_s)

    write_text(output_path, "\n".join(lines) + "\n")

    return {
        "ok": True,
        "technique": "lsv",
        "output_path": str(output_path),
        "points": len(times),
    }


def run_cv(
    step: dict[str, Any],
    output_path: str | Path,
    emitter: LiveEmitter | None = None,
) -> dict[str, Any]:
    initial_v = as_float(step.get("initial_voltage_v"), 0)
    first_v = as_float(step.get("first_vertex_v", step.get("apex1_voltage_v")), 1)
    second_v = as_float(step.get("second_vertex_v", step.get("apex2_voltage_v")), -1)
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
            absolute_time = current_time + t
            rows.append((absolute_time, potential, current))
            if emitter:
                emitter.emit("cv", {"t_s": absolute_time, "e_v": potential, "i_a": current})
                emitter.wait(sample_period_s)

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
        "points": len(rows),
    }


def logspace_values(start: float, stop: float, points_per_decade: int) -> list[float]:
    start = max(start, 1e-12)
    stop = max(stop, 1e-12)
    points_per_decade = max(1, int(points_per_decade))

    log_start = math.log10(start)
    log_stop = math.log10(stop)
    total_points = int(abs(log_start - log_stop) * points_per_decade) + 1
    total_points = max(total_points, 2)

    return [
        10 ** (log_start + (log_stop - log_start) * i / (total_points - 1))
        for i in range(total_points)
    ]


def run_eis(
    step: dict[str, Any],
    output_path: str | Path,
    emitter: LiveEmitter | None = None,
) -> dict[str, Any]:
    initial_frequency_hz = as_float(
        step.get("initial_frequency_hz", step.get("initial_freq_hz")),
        100000,
    )
    final_frequency_hz = as_float(
        step.get("final_frequency_hz", step.get("final_freq_hz")),
        0.1,
    )
    points_per_decade = as_int(step.get("points_per_decade"), 10)
    frequencies = logspace_values(initial_frequency_hz, final_frequency_hz, points_per_decade)

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
        if emitter:
            emitter.emit(
                "eis",
                {
                    "freq_hz": freq,
                    "zreal_ohm": zreal,
                    "zimag_ohm": zimag,
                    "zmod_ohm": zmod,
                    "phase_deg": phase,
                },
            )
            emitter.wait(0.1)

    write_text(output_path, "\n".join(lines) + "\n")

    return {
        "ok": True,
        "technique": "eis",
        "output_path": str(output_path),
        "points": len(frequencies),
    }


def run_geis(
    step: dict[str, Any],
    output_path: str | Path,
    emitter: LiveEmitter | None = None,
) -> dict[str, Any]:
    geis_step = dict(step)
    estimated_z = as_float(step.get("estimated_z_ohm"), 100)
    dc_current = as_float(step.get("dc_current_a"), 0)
    ac_current = as_float(step.get("ac_current_a"), 1e-5)
    initial_frequency_hz = as_float(step.get("initial_frequency_hz", step.get("initial_freq_hz")), 100000)
    final_frequency_hz = as_float(step.get("final_frequency_hz", step.get("final_freq_hz")), 0.1)
    points_per_decade = as_int(step.get("points_per_decade"), 10)
    if ac_current <= 0:
        raise MockGamryError("GEIS ac_current_a must be positive.")
    if initial_frequency_hz <= 0 or final_frequency_hz <= 0 or initial_frequency_hz == final_frequency_hz:
        raise MockGamryError("GEIS frequencies must be positive and different.")
    if estimated_z <= 0 or points_per_decade < 1:
        raise MockGamryError("GEIS estimated_z_ohm and points_per_decade must be positive.")
    frequencies = logspace_values(initial_frequency_hz, final_frequency_hz, points_per_decade)
    lines = header(geis_step, "geis")
    lines.append("Pt\tT\tFreq\tZreal\tZimag\tZmod\tZphz\tIdc\tVdc")

    for point, freq in enumerate(frequencies):
        omega = 2 * math.pi * freq
        rs = 20.0
        cdl = 2e-5
        denom = 1 + (omega * estimated_z * cdl) ** 2
        zreal = rs + estimated_z / denom
        zimag = -(omega * estimated_z * estimated_z * cdl) / denom
        zmod = math.sqrt(zreal * zreal + zimag * zimag)
        phase = math.degrees(math.atan2(zimag, zreal))
        elapsed_s = point * 0.1
        dc_voltage = dc_current * zreal
        lines.append(f"{point}\t{elapsed_s:.6f}\t{freq:.9e}\t{zreal:.9f}\t{zimag:.9f}\t{zmod:.9f}\t{phase:.9f}\t{dc_current:.12e}\t{dc_voltage:.12e}")
        if emitter:
            emitter.emit(
                "geis",
                {
                    "freq_hz": freq,
                    "zreal_ohm": zreal,
                    "zimag_ohm": zimag,
                    "zmod_ohm": zmod,
                    "phase_deg": phase,
                },
            )
            emitter.wait(0.1)

    write_text(output_path, "\n".join(lines) + "\n")
    return {
        "ok": True,
        "technique": "geis",
        "output_path": str(output_path),
        "initial_frequency_hz": initial_frequency_hz,
        "final_frequency_hz": final_frequency_hz,
        "points": len(frequencies),
        "elapsed_s": max(0, len(frequencies) - 1) * 0.1,
        "stop_reason": "frequency_sweep_complete",
    }


def _output_records(step: dict[str, Any], outputs: list[Any]) -> list[dict[str, Any]]:
    records = []
    start_voltage_v = as_float(step.get("start_voltage_v"), -0.1)
    step_voltage_v = as_float(step.get("step_voltage_v"), -0.1)

    for index, output in enumerate(outputs, start=1):
        if isinstance(output, dict):
            record = dict(output)
            record["path"] = str(record.get("path") or record.get("output") or "")
        else:
            record = {"path": str(output)}
        record["index"] = as_int(record.get("index"), index)
        record.setdefault(
            "voltage_v",
            start_voltage_v + step_voltage_v * (record["index"] - 1),
        )
        records.append(record)
    return records


def run_ca_staircase(
    step: dict[str, Any],
    outputs: list[Any],
    emitter: LiveEmitter | None = None,
) -> dict[str, Any]:
    results = []
    time_offset_s = 0.0

    for output in _output_records(step, outputs):
        voltage = float(output["voltage_v"])
        ca_step = {
            **step,
            "technique": "ca",
            "voltage_v": voltage,
            "duration_s": step.get("step_time_s", 300),
        }
        result = run_ca(
            ca_step,
            output["path"],
            voltage_v=voltage,
            emitter=emitter,
            time_offset_s=time_offset_s,
        )
        results.append(result)
        time_offset_s += float(result.get("duration_s", 0))

    return {"ok": True, "technique": "ca_staircase", "outputs": results}


def run_step(
    step: dict[str, Any],
    outputs: list[Any],
    emitter: LiveEmitter | None = None,
) -> dict[str, Any]:
    if not outputs:
        raise MockGamryError("outputs cannot be empty.")

    technique = str(step.get("technique", "")).lower().strip()
    records = _output_records(step, outputs)

    if technique == "ocp":
        return run_ocp(step, records[0]["path"], emitter=emitter)
    if technique == "ca":
        return run_ca(step, records[0]["path"], emitter=emitter)
    if technique == "cp":
        return run_cp(step, records[0]["path"], emitter=emitter)
    if technique in {"cc_charge", "cc_discharge"}:
        return run_cc(step, records[0]["path"], technique=technique, emitter=emitter)
    if technique == "ca_staircase":
        return run_ca_staircase(step, records, emitter=emitter)
    if technique == "cv":
        return run_cv(step, records[0]["path"], emitter=emitter)
    if technique == "lsv":
        return run_lsv(step, records[0]["path"], emitter=emitter)
    if technique == "eis":
        return run_eis(step, records[0]["path"], emitter=emitter)
    if technique == "geis":
        return run_geis(step, records[0]["path"], emitter=emitter)

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

    gamry_config = job.get("gamry", {})
    if not isinstance(gamry_config, dict):
        gamry_config = {}
    live_config = gamry_config.get("live_plot", {})
    if not isinstance(live_config, dict):
        live_config = {}

    live_enabled = bool(job.get("live_enabled", live_config.get("enabled", True)))
    emitter = LiveEmitter(
        job.get("live_dir") if live_enabled else None,
        mock_time_scale=as_float(live_config.get("mock_time_scale"), 0.05),
    )
    result = run_step(step, outputs, emitter=emitter)

    return {
        "ok": True,
        "mode": "mock",
        "created_at": now_iso(),
        "result": result,
    }
