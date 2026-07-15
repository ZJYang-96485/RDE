from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


class RealGamryError(RuntimeError):
    pass


def webui_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_path(value: Any, base_dir: Path) -> Path:
    text = str(value or "").strip()

    if not text:
        raise RealGamryError("real Gamry runner path cannot be empty.")

    path = Path(text)

    if not path.is_absolute():
        path = base_dir / path

    return path


def normalize_command(value: Any) -> list[str]:
    if value is None or value == "":
        return []

    if isinstance(value, list):
        command = [str(item) for item in value if str(item).strip()]
    else:
        command = shlex.split(str(value), posix=(os.name != "nt"))

    if not command:
        return []

    return command


def real_result_path(job: dict[str, Any]) -> Path:
    configured = str(job.get("result_path", "") or "").strip()

    if configured:
        result_path = Path(configured)
        return result_path.with_name(result_path.stem + "_real_result.json")

    job_path = Path(str(job.get("_job_path", "real_gamry_job.json")))
    return job_path.with_name(job_path.stem + "_real_result.json")


def build_real_command(job: dict[str, Any], result_path: Path) -> list[str]:
    config = job.get("gamry", {})

    if not isinstance(config, dict):
        raise RealGamryError("job.gamry must be an object for real Gamry mode.")

    command = normalize_command(config.get("real_worker_command"))

    if not command:
        script_value = config.get("real_worker_script") or config.get("external_runner")

        if script_value:
            script = resolve_path(script_value, webui_root())
            python = str(config.get("real_worker_python") or "").strip()

            if not python:
                python = sys.executable if script.suffix.lower() == ".py" else ""

            command = [python, str(script)] if python else [str(script)]

    if not command:
        raise RealGamryError(
            "real Gamry mode needs gamry.real_worker_script or "
            "gamry.real_worker_command in config.json. The runner must accept "
            "--job <job.json> --result <result.json> and create the requested DTA outputs."
        )

    job_path = str(job.get("_job_path", "") or "").strip()

    if not job_path:
        raise RealGamryError("real Gamry job path is missing.")

    return command + ["--job", job_path, "--result", str(result_path)]


def read_result(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RealGamryError(f"real Gamry runner did not write result file: {path}")

    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, dict):
        raise RealGamryError(f"real Gamry result must be a JSON object: {path}")

    return payload


def verify_outputs(outputs: list[str]) -> None:
    missing = [
        output
        for output in outputs
        if not Path(output).exists()
    ]

    if missing:
        raise RealGamryError(
            "real Gamry runner finished but did not create output file(s): "
            + ", ".join(missing)
        )


def run_external_worker(job: dict[str, Any], outputs: list[str]) -> dict[str, Any]:
    config = job.get("gamry", {})

    if not isinstance(config, dict):
        config = {}

    result_path = real_result_path(job)
    command = build_real_command(job, result_path)
    timeout_s = float(config.get("real_timeout_s", 7200))

    try:
        completed = subprocess.run(
            command,
            cwd=str(webui_root()),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise RealGamryError(f"real Gamry runner timed out after {timeout_s}s.") from exc
    except Exception as exc:
        raise RealGamryError(f"unable to start real Gamry runner: {exc}") from exc

    result = read_result(result_path)
    result.setdefault("stdout", completed.stdout)
    result.setdefault("stderr", completed.stderr)
    result.setdefault("returncode", completed.returncode)

    if completed.returncode != 0 or not bool(result.get("ok", False)):
        error = result.get("error") or completed.stderr or "real Gamry runner failed."
        raise RealGamryError(str(error))

    verify_outputs(outputs)

    return {
        "ok": True,
        "backend": "external",
        "command": command,
        "result_path": str(result_path),
        "outputs": outputs,
        "runner": result,
    }


def run(
    job: dict[str, Any],
    step: dict[str, Any],
    outputs: list[str],
    sample_id: str | None = None,
) -> dict[str, Any]:
    technique = str(step.get("technique", "")).strip().lower()

    if technique not in {
        "ocp",
        "ca",
        "ca_staircase",
        "cv",
        "lsv",
        "eis",
        "cp",
        "cc_charge",
        "cc_discharge",
        "geis",
    }:
        raise RealGamryError(f"unsupported real Gamry technique: {technique}")

    return run_external_worker(job=job, outputs=outputs)
