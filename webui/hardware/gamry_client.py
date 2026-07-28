from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workflow.config_loader import get_gamry_config
from gamry_worker.live_writer import fail_live_stream


class GamryClientError(RuntimeError):
    def __init__(self, message: str, *, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.result = result


def best_effort_live_failure(job: dict[str, Any], message: str) -> None:
    if not job.get("live_enabled", False):
        return
    try:
        fail_live_stream(job["live_dir"], message)
    except Exception:
        # Live plotting is observational and must never mask acquisition errors.
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
        self._process_lock = threading.RLock()
        self._active_process: subprocess.Popen[str] | None = None
        self._active_job_id: str | None = None
        self._emergency_disconnect_generation = 0
        self._emergency_disconnect_active = False

    def active_worker_status(self) -> dict[str, Any]:
        with self._process_lock:
            process = self._active_process
            active = bool(process is not None and process.poll() is None)
            return {
                "active": active,
                "pid": process.pid if active and process is not None else None,
                "job_id": self._active_job_id if active else None,
                "emergency_disconnect_active": self._emergency_disconnect_active,
            }

    @staticmethod
    def _terminate_process(
        process: subprocess.Popen[str],
        timeout_s: float = 2.0,
    ) -> tuple[bool, bool]:
        """Terminate one worker and force-kill it only if it will not exit."""
        if process.poll() is not None:
            return False, False

        process.terminate()
        try:
            process.wait(timeout=max(0.1, float(timeout_s)))
            return True, False
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=max(0.1, float(timeout_s)))
            return True, True

    def disconnect_active_worker(self) -> dict[str, Any]:
        """
        Disconnect the active acquisition worker during an emergency stop.

        The returned generation token prevents an older recovery thread from
        clearing the disconnect state for a newer emergency stop.
        """
        with self._process_lock:
            self._emergency_disconnect_generation += 1
            generation = self._emergency_disconnect_generation
            self._emergency_disconnect_active = True
            process = self._active_process
            job_id = self._active_job_id

            if process is None or process.poll() is not None:
                return {
                    "ok": True,
                    "generation": generation,
                    "worker_was_active": False,
                    "worker_terminated": False,
                    "worker_force_killed": False,
                    "pid": None,
                    "job_id": job_id,
                }

            pid = process.pid
            terminated, force_killed = self._terminate_process(process)
            return {
                "ok": True,
                "generation": generation,
                "worker_was_active": True,
                "worker_terminated": terminated,
                "worker_force_killed": force_killed,
                "pid": pid,
                "job_id": job_id,
            }

    def finish_emergency_disconnect(self, generation: int) -> bool:
        with self._process_lock:
            if int(generation) != self._emergency_disconnect_generation:
                return False
            self._emergency_disconnect_active = False
            return True

    def emergency_disconnect_in_progress(self) -> bool:
        with self._process_lock:
            return bool(self._emergency_disconnect_active)

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
        configured = str(
            self.config().get("worker_script", "gamry_worker/worker.py") or ""
        ).strip()
        path = Path(configured)

        if not path.is_absolute():
            path = self.root / path

        if not path.exists():
            raise GamryClientError(f"Gamry worker script does not exist: {path}")

        return path

    def runtime_status(self) -> dict[str, Any]:
        python = self.worker_python()
        python_path = Path(python)
        python_exists = python_path.is_file() or shutil.which(python) is not None

        configured_script = str(
            self.config().get("worker_script", "gamry_worker/worker.py") or ""
        ).strip()
        script_path = Path(configured_script)

        if not script_path.is_absolute():
            script_path = self.root / script_path

        script_exists = script_path.is_file()

        return {
            "configured": bool(python_exists and script_exists),
            "worker_python": python,
            "worker_python_exists": bool(python_exists),
            "worker_script": str(script_path),
            "worker_script_exists": bool(script_exists),
        }

    def job_dir(self, run_dir: str | Path) -> Path:
        # Internal worker files are grouped in one hidden/system folder so the
        # run root contains only sample folders plus the user-facing summary.
        path = Path(run_dir) / "_system" / "jobs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def build_job(
        self,
        step: dict[str, Any],
        outputs: list[str | Path],
        run_dir: str | Path,
        sample_id: str | None = None,
        sample_label: str | None = None,
        protocol_name: str | None = None,
    ) -> tuple[dict[str, Any], Path, Path]:
        if not isinstance(step, dict):
            raise GamryClientError("step must be an object.")

        job_id = f"{utc_now_compact()}_{uuid.uuid4().hex[:10]}"
        jobs_dir = self.job_dir(run_dir)
        job_path = jobs_dir / f"{job_id}_job.json"
        result_path = jobs_dir / f"{job_id}_result.json"
        run_path = Path(run_dir)
        live_config = self.config().get("live_plot", {})
        if not isinstance(live_config, dict):
            live_config = {}

        job = {
            "job_id": job_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode(),
            "run_id": run_path.name,
            "run_dir": str(run_path),
            "live_dir": str(run_path / "_system" / "live"),
            "live_enabled": bool(live_config.get("enabled", True)),
            "sample_id": sample_id,
            "sample_label": sample_label,
            "protocol_name": protocol_name,
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
        sample_label: str | None = None,
        protocol_name: str | None = None,
    ) -> dict[str, Any]:
        job, job_path, result_path = self.build_job(
            step=step,
            outputs=outputs,
            run_dir=run_dir,
            sample_id=sample_id,
            sample_label=sample_label,
            protocol_name=protocol_name,
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

        process: subprocess.Popen[str] | None = None
        stdout = ""
        stderr = ""
        try:
            # Hold the registration lock across process creation so an
            # emergency disconnect cannot miss a worker that is just starting.
            with self._process_lock:
                active = self._active_process
                if active is not None and active.poll() is None:
                    raise GamryClientError(
                        "another Gamry acquisition worker is already active."
                    )
                process = subprocess.Popen(
                    command,
                    cwd=str(self.root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self._active_process = process
                self._active_job_id = str(job.get("job_id") or "")

            stdout, stderr = process.communicate(
                timeout=float(self.config().get("real_timeout_s", 7200))
            )
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                self._terminate_process(process)
                stdout, stderr = process.communicate()
            message = f"Gamry worker timed out after {exc.timeout} seconds."
            best_effort_live_failure(job, message)
            raise GamryClientError(message) from exc
        except GamryClientError:
            raise
        except Exception as exc:
            message = f"unable to start Gamry worker: {exc}"
            best_effort_live_failure(job, message)
            raise GamryClientError(message) from exc
        finally:
            if process is not None:
                with self._process_lock:
                    if self._active_process is process:
                        self._active_process = None
                        self._active_job_id = None

        returncode = process.returncode
        if returncode is None:
            returncode = process.wait()

        if result_path.exists():
            result = read_json(result_path)
        else:
            result = {
                "ok": False,
                "error": "Gamry worker did not create a result file.",
                "stdout": stdout,
                "stderr": stderr,
                "returncode": returncode,
            }

        result["client"] = {
            "job_path": str(job_path),
            "result_path": str(result_path),
            "worker_script": str(self.worker_script()),
            "worker_python": self.worker_python(),
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
        }

        if returncode != 0 or not bool(result.get("ok", False)):
            error = result.get("error") or stderr or "Gamry worker failed."
            details = [str(error)]
            if stdout.strip():
                details.append("Worker stdout:\n" + stdout.strip())
            if stderr.strip() and stderr.strip() != str(error).strip():
                details.append("Worker stderr:\n" + stderr.strip())
            message = "\n".join(details)
            best_effort_live_failure(job, message)
            raise GamryClientError(message, result=result)

        trial_metadata = result.get("trial_metadata", {})
        skipped = (
            isinstance(trial_metadata, dict)
            and str(trial_metadata.get("trial_status", "")).strip().lower() == "skipped"
        )
        missing_outputs = [] if skipped else [
            output for output in job["outputs"] if not Path(output).is_file()
        ]

        if missing_outputs:
            message = "Gamry worker reported success but did not create: " + ", ".join(missing_outputs)
            best_effort_live_failure(job, message)
            raise GamryClientError(message)

        return result

    def probe(self) -> dict[str, Any]:
        runtime = self.runtime_status()

        if not runtime["configured"]:
            missing = []

            if not runtime["worker_python_exists"]:
                missing.append(f"Python runtime: {runtime['worker_python']}")

            if not runtime["worker_script_exists"]:
                missing.append(f"worker script: {runtime['worker_script']}")

            raise GamryClientError("Gamry runtime is not ready; missing " + "; ".join(missing))

        command = [
            self.worker_python(),
            str(self.worker_script()),
            "--probe",
        ]

        try:
            completed = subprocess.run(
                command,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                check=False,
                timeout=float(self.config().get("probe_timeout_s", 15)),
            )
        except subprocess.TimeoutExpired as exc:
            raise GamryClientError(
                f"Gamry device check timed out after {exc.timeout} seconds."
            ) from exc
        except Exception as exc:
            raise GamryClientError(f"unable to start Gamry device check: {exc}") from exc

        probe_output = completed.stdout.strip() or completed.stderr.strip()

        try:
            result = json.loads(probe_output)
        except json.JSONDecodeError as exc:
            raise GamryClientError(
                "Gamry device check returned invalid JSON. "
                f"stderr: {completed.stderr.strip()}"
            ) from exc

        if not isinstance(result, dict):
            raise GamryClientError("Gamry device check result must be a JSON object.")

        if completed.returncode != 0 or not bool(result.get("ok", False)):
            error = result.get("error") or completed.stderr or "Gamry device check failed."
            raise GamryClientError(str(error))

        sections = [
            str(section).strip()
            for section in result.get("sections", [])
            if str(section).strip()
        ]
        gamry = self.config()
        configured_label = str(gamry.get("instrument_label", "") or "").strip()
        configured_index = int(gamry.get("instrument_index", 0))
        selected_instrument = None

        if configured_label:
            if configured_label in sections:
                selected_instrument = configured_label
        elif 0 <= configured_index < len(sections):
            selected_instrument = sections[configured_index]

        result.update(
            {
                "connected": selected_instrument is not None,
                "configured_instrument_label": configured_label,
                "configured_instrument_index": configured_index,
                "selected_instrument": selected_instrument,
                "runtime": runtime,
                "stderr": completed.stderr,
            }
        )
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
    sample_label: str | None = None,
    protocol_name: str | None = None,
) -> dict[str, Any]:
    return get_gamry_client().run_step(
        step=step,
        outputs=outputs,
        run_dir=run_dir,
        sample_id=sample_id,
        sample_label=sample_label,
        protocol_name=protocol_name,
    )
