from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workflow.config_loader import get_gamry_config, get_path


class DataManagerError(RuntimeError):
    pass


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_name(value: Any, fallback: str = "item") -> str:
    text = str(value or "").strip()

    if not text:
        text = fallback

    text = re.sub(r"[^A-Za-z0-9 _.-]", "_", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._-")

    if not text:
        text = fallback

    return text


def output_root() -> Path:
    path = get_path("output_dir")
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_extension() -> str:
    extension = str(get_gamry_config().get("default_file_extension", ".DTA") or ".DTA").strip()

    if not extension.startswith("."):
        extension = "." + extension

    return extension


def ensure_extension(filename: str, extension: str | None = None) -> str:
    filename = str(filename or "").strip()

    if not filename:
        filename = "output"

    if extension is None:
        extension = default_extension()

    if Path(filename).suffix:
        return filename

    return filename + extension


def make_run_id(run_plan: dict[str, Any]) -> str:
    name = safe_name(run_plan.get("run_name") or run_plan.get("display_name") or "run")
    return f"{compact_timestamp()}_{name}"


def make_run_dir(run_plan: dict[str, Any]) -> Path:
    run_dir = output_root() / make_run_id(run_plan)
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def read_json(path: str | Path) -> dict[str, Any]:
    path = Path(path)

    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, dict):
        raise DataManagerError(f"JSON file must contain an object: {path}")

    return payload


def append_log(run_dir: str | Path, message: str) -> None:
    run_dir = Path(run_dir)
    log_path = run_dir / "log.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{utc_timestamp()}] {message}\n")


def manifest_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "manifest.json"


def load_manifest(run_dir: str | Path) -> dict[str, Any]:
    path = manifest_path(run_dir)

    if not path.exists():
        return {
            "status": "created",
            "created_at": utc_timestamp(),
            "completed_at": None,
            "samples": [],
            "protocols": [],
            "outputs": [],
            "errors": []
        }

    return read_json(path)


def save_manifest(run_dir: str | Path, manifest: dict[str, Any]) -> None:
    write_json(manifest_path(run_dir), manifest)


def create_run_workspace(run_plan: dict[str, Any]) -> dict[str, Any]:
    run_dir = make_run_dir(run_plan)

    write_json(run_dir / "run_plan.json", run_plan)

    manifest = {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "run_name": run_plan.get("run_name"),
        "display_name": run_plan.get("display_name"),
        "status": "running",
        "created_at": utc_timestamp(),
        "completed_at": None,
        "samples": [],
        "protocols": [],
        "outputs": [],
        "errors": []
    }

    save_manifest(run_dir, manifest)
    append_log(run_dir, "Run workspace created.")

    return {
        "run_dir": run_dir,
        "manifest": manifest
    }


def sample_dir_name(sample: dict[str, Any], sample_index: int) -> str:
    sample_id = sample.get("sample_id") or f"sample_{sample_index:03d}"
    label = sample.get("label") or sample_id
    return f"{sample_index:03d}_{safe_name(sample_id)}_{safe_name(label)}"


def create_sample_workspace(
    run_dir: str | Path,
    sample: dict[str, Any],
    sample_index: int,
) -> Path:
    run_dir = Path(run_dir)
    sample_dir = run_dir / "samples" / sample_dir_name(sample, sample_index)
    sample_dir.mkdir(parents=True, exist_ok=True)

    write_json(sample_dir / "sample.json", sample)

    manifest = load_manifest(run_dir)
    sample_record = {
        "sample_index": sample_index,
        "sample_id": sample.get("sample_id"),
        "label": sample.get("label"),
        "sample_dir": str(sample_dir),
        "created_at": utc_timestamp()
    }

    existing = [
        item for item in manifest.get("samples", [])
        if int(item.get("sample_index", -1)) != int(sample_index)
    ]
    existing.append(sample_record)
    manifest["samples"] = existing

    save_manifest(run_dir, manifest)
    append_log(run_dir, f"Sample workspace created: {sample_dir}")

    return sample_dir


def voltage_label(voltage: float) -> str:
    sign = "p" if voltage >= 0 else "m"
    value = abs(float(voltage))
    text = f"{value:.3f}".replace(".", "p")
    return sign + text + "V"


def normal_step_filename(step: dict[str, Any], step_index: int) -> str:
    output = step.get("output")

    if output:
        return safe_name(ensure_extension(str(output)))

    name = step.get("name") or f"step_{step_index:03d}"
    technique = step.get("technique") or "echem"
    filename = f"{step_index:03d}_{safe_name(technique)}_{safe_name(name)}"
    return ensure_extension(filename)


def staircase_step_filename(
    step: dict[str, Any],
    step_index: int,
    sub_index: int,
    voltage: float,
) -> str:
    prefix = step.get("output_prefix") or step.get("name") or f"step_{step_index:03d}"
    filename = f"{step_index:03d}_{safe_name(prefix)}_{sub_index:03d}_{voltage_label(voltage)}"
    return ensure_extension(filename)


def build_step_outputs(
    sample_dir: str | Path,
    step: dict[str, Any],
    step_index: int,
) -> list[str]:
    sample_dir = Path(sample_dir)
    technique = str(step.get("technique", "")).strip().lower()

    if technique == "ca_staircase":
        start_voltage = float(step.get("start_voltage_v", 0))
        step_voltage = float(step.get("step_voltage_v", 0))
        step_count = int(step.get("step_count", 1))

        outputs = []

        for sub_index in range(1, step_count + 1):
            voltage = start_voltage + (sub_index - 1) * step_voltage
            filename = staircase_step_filename(step, step_index, sub_index, voltage)
            outputs.append(str(sample_dir / filename))

        return outputs

    filename = normal_step_filename(step, step_index)
    return [str(sample_dir / filename)]


def register_step_outputs(
    run_dir: str | Path,
    sample_dir: str | Path,
    sample_index: int,
    step: dict[str, Any],
    step_index: int,
    outputs: list[str],
) -> dict[str, Any]:
    run_dir = Path(run_dir)

    record = {
        "sample_index": sample_index,
        "sample_dir": str(sample_dir),
        "step_index": step_index,
        "step_name": step.get("name"),
        "technique": step.get("technique"),
        "outputs": outputs,
        "created_at": utc_timestamp()
    }

    manifest = load_manifest(run_dir)
    manifest.setdefault("outputs", []).append(record)
    save_manifest(run_dir, manifest)

    return record


def prepare_protocol_outputs(
    run_dir: str | Path,
    sample_dir: str | Path,
    sample_index: int,
    protocol: dict[str, Any],
) -> list[dict[str, Any]]:
    records = []

    for step_index, step in enumerate(protocol.get("steps", []), start=1):
        if not bool(step.get("enabled", True)):
            continue

        outputs = build_step_outputs(sample_dir, step, step_index)

        record = register_step_outputs(
            run_dir=run_dir,
            sample_dir=sample_dir,
            sample_index=sample_index,
            step=step,
            step_index=step_index,
            outputs=outputs,
        )

        records.append(record)

    return records


def save_protocol_snapshot(run_dir: str | Path, protocol: dict[str, Any]) -> Path:
    run_dir = Path(run_dir)
    protocol_name = safe_name(protocol.get("protocol_name") or protocol.get("display_name") or "protocol")
    snapshot_dir = run_dir / "protocol_snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    path = snapshot_dir / f"{compact_timestamp()}_{protocol_name}.json"
    write_json(path, protocol)

    manifest = load_manifest(run_dir)
    manifest.setdefault("protocols", []).append(
        {
            "protocol_name": protocol.get("protocol_name"),
            "display_name": protocol.get("display_name"),
            "path": str(path),
            "saved_at": utc_timestamp()
        }
    )
    save_manifest(run_dir, manifest)

    return path


def mark_run_complete(run_dir: str | Path) -> None:
    manifest = load_manifest(run_dir)
    manifest["status"] = "complete"
    manifest["completed_at"] = utc_timestamp()
    save_manifest(run_dir, manifest)
    append_log(run_dir, "Run marked complete.")


def mark_run_failed(run_dir: str | Path, error: str) -> None:
    manifest = load_manifest(run_dir)
    manifest["status"] = "failed"
    manifest["completed_at"] = utc_timestamp()
    manifest.setdefault("errors", []).append(
        {
            "error": str(error),
            "time": utc_timestamp()
        }
    )
    save_manifest(run_dir, manifest)
    append_log(run_dir, f"Run marked failed: {error}")


def mark_run_aborted(run_dir: str | Path) -> None:
    manifest = load_manifest(run_dir)
    manifest["status"] = "aborted"
    manifest["completed_at"] = utc_timestamp()
    save_manifest(run_dir, manifest)
    append_log(run_dir, "Run marked aborted.")