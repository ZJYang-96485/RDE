from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workflow.config_loader import get_gamry_config, load_config


class GamryClientError(RuntimeError):
    pass


def webui_root() -> Path:
    return Path(__file__).resolve().parents[1]


def utc_now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, dict):
        raise GamryClientError(f"JSON file is not an object: {path}")

    return payload


def normalize_output_paths(outputs: list[str | Path]) -> list[str]:
    normalized = []

    for output in outputs:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        normalized.append(str(output_path))

    if not normalized:
        raise GamryClientError("at least one output path is required for a Gamry step.")

    return normalized


class GamryClient:
    def __init__(self) -> None:
        self.root = webui_root()

    def config(self) -> dict[str, Any]:
        return get_gamry_config()

    def mode(self) -> str:
        return str(self.config().get("mode", "mock")).strip().lower()

    def worker_python(self) -> str:
        configured = str(self.config().get("worker_python", "") or "").strip()

        if configured:
            return configured

        return sys.executable

    def worker_script(self) -> Path:
        configured = str(self.config().get("worker_script", "gamry_worker/worker.py") or "").strip()
        path = Path(configured)

        if not path.is_absolute():
            path = self.root / path

        if not path.exists():
            raise GamryClientError(f"Gamry worker script does not exist: {path}")

        return path

    def job_dir(self, run_dir: str | Path) -> Path:
        path = Path(run_dir) / "_jobs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def build_job(
        self,
        step: dict[str, Any],
        outputs: list[str | Path],
        run_dir: str | Path,
        sample_id: str | None = None,
    ) -> tuple[dict[str, Any], Path, Path]:
        if not isinstance(step, dict):
            raise GamryClientError("step must be an object.")

        job_id = f"{utc_now_compact()}_{uuid.uuid4().hex[:10]}"
        jobs_dir = self.job_dir(run_dir)

        job_path = jobs_dir / f"{job_id}_job.json"
        result_path = jobs_dir / f"{job_id}_result.json"

        job = {
            "job_id": job_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode(),
            "sample_id": sample_id,
            "step": step,
            "outputs": normalize_output_paths(outputs),
            "result_path": str(result_path),
            "gamry": self.config(),
        }

        return job, job_path, result_path

    def run_step(
        self,
        step: dict[str, Any],
        outputs: list[str | Path],
        run_dir: str | Path,
        sample_id: str | None = None,
    ) -> dict[str, Any]:
        job, job_path, result_path = self.build_job(
            step=step,
            outputs=outputs,
            run_dir=run_dir,
            sample_id=sample_id,
        )

        write_json(job_path, job)

        command = [
            self.worker_python(),
            str(self.worker_script()),
            "--job",
            str(job_path),
            "--result",
            str(result_path),
        ]

        try:
            completed = subprocess.run(
                command,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception as exc:
            raise GamryClientError(f"unable to start Gamry worker: {exc}") from exc

        if result_path.exists():
            result = read_json(result_path)
        else:
            result = {
                "ok": False,
                "error": "Gamry worker did not create a result file.",
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "returncode": completed.returncode,
            }

        result["client"] = {
            "job_path": str(job_path),
            "result_path": str(result_path),
            "worker_script": str(self.worker_script()),
            "worker_python": self.worker_python(),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

        if completed.returncode != 0 or not bool(result.get("ok", False)):
            error = result.get("error") or completed.stderr or "Gamry worker failed."
            raise GamryClientError(str(error))

        return result


_default_gamry_client: GamryClient | None = None


def get_gamry_client() -> GamryClient:
    global _default_gamry_client

    if _default_gamry_client is None:
        _default_gamry_client = GamryClient()

    return _default_gamry_client


def run_gamry_step(
    step: dict[str, Any],
    outputs: list[str | Path],
    run_dir: str | Path,
    sample_id: str | None = None,
) -> dict[str, Any]:
    return get_gamry_client().run_step(
        step=step,
        outputs=outputs,
        run_dir=run_dir,
        sample_id=sample_id,
    )