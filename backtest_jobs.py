"""Durable local job manager for isolated Kuru LP backtests."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backtest_service import ROOT, atomic_write_bytes, canonical_bytes, resolve_node
from sdk_grid_broker import SdkGridBroker


JOB_ROOT = ROOT / ".cache" / "backtest_jobs"
TERMINAL_STATES = {"completed", "failed", "canceled", "stalled"}
STALL_SECONDS = 120
LIVE_STATUS_ROOT = Path(tempfile.gettempdir()) / "kuru_backtest_live_status"


def live_status_path(run_id: str) -> Path:
    """Return the low-latency path for nonterminal worker heartbeats."""
    return LIVE_STATUS_ROOT / f"{run_id}.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class JobError(RuntimeError):
    pass


class BacktestJobManager:
    def __init__(self, root: Path = JOB_ROOT, maximum_workers: int = 2) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.Lock()
        self._admission_lock = threading.Lock()
        self.maximum_workers = maximum_workers
        self._closed = False
        self._sdk_broker: SdkGridBroker | None = None

    def _ensure_sdk_broker(self) -> SdkGridBroker:
        with self._lock:
            if self._sdk_broker is None:
                self._sdk_broker = SdkGridBroker(
                    resolve_node(),
                    ROOT / "sdk_reference" / "generate_grid_worker.js",
                    ROOT,
                )
            return self._sdk_broker

    def close(self) -> None:
        """Stop owned workers before closing their shared SDK broker."""
        with self._admission_lock:
            self._closed = True
            for run_id, process in tuple(self._processes.items()):
                if process.poll() is None:
                    status = self._read_status(run_id)
                    atomic_write_bytes(self._directory(run_id) / "cancel.requested", b"cancel\n")
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    status.update(status="canceled", stage="canceled", canceled_at=utc_now(), failure_reason="server shut down")
                    self._write_status(run_id, status)
                    live_status_path(run_id).unlink(missing_ok=True)
        with self._lock:
            broker = self._sdk_broker
            self._sdk_broker = None
        if broker is not None:
            broker.close()

    def _directory(self, run_id: str) -> Path:
        if not run_id.startswith("job-") or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in run_id):
            raise JobError("invalid run ID")
        return self.root / run_id

    def _read_status(self, run_id: str) -> dict[str, Any]:
        path = self._directory(run_id) / "status.json"
        if not path.exists():
            raise JobError("unknown run ID")
        durable = json.loads(path.read_text(encoding="utf-8"))
        live_path = live_status_path(run_id)
        if durable.get("status") in TERMINAL_STATES or not live_path.exists():
            return durable
        # Retry if a read occurs during a small, direct heartbeat write in the
        # native temporary file system. Terminal state stays in the job folder.
        for attempt in range(3):
            try:
                live = json.loads(live_path.read_text(encoding="utf-8"))
                if live.get("run_id") != run_id:
                    raise JobError("live job status has an invalid run ID")
                return live
            except json.JSONDecodeError:
                if attempt == 2:
                    return durable
                time.sleep(0.01)
        return durable

    def _write_status(self, run_id: str, status: dict[str, Any]) -> None:
        atomic_write_bytes(self._directory(run_id) / "status.json", canonical_bytes(status) + b"\n")

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._admission_lock:
            if self._closed:
                raise JobError("server is shutting down")
            with self._lock:
                self._processes = {key: process for key, process in self._processes.items() if process.poll() is None}
                if len(self._processes) >= self.maximum_workers:
                    raise JobError("maximum active backtest count reached; wait for a run to finish")
            return self._create(request)

    def _create(self, request: dict[str, Any]) -> dict[str, Any]:
        broker = self._ensure_sdk_broker()
        created = utc_now()
        request_sha = hashlib.sha256(canonical_bytes(request)).hexdigest()
        run_id = f"job-{int(time.time() * 1000):x}-{secrets.token_hex(5)}"
        owner_token = secrets.token_urlsafe(24)
        directory = self._directory(run_id)
        directory.mkdir(parents=True, exist_ok=False)
        live_status_path(run_id).unlink(missing_ok=True)
        atomic_write_bytes(directory / "request.json", canonical_bytes(request) + b"\n")
        status = {
            "schema_version": "1.0",
            "run_id": run_id,
            "owner_token_sha256": hashlib.sha256(owner_token.encode()).hexdigest(),
            "request_sha256": request_sha,
            "status": "queued",
            "stage": "queued",
            "created_at": created,
            "job_started_at": None,
            "last_progress_at": created,
            "completed_at": None,
            "failed_at": None,
            "canceled_at": None,
            "stalled_at": None,
            "processed_events": 0,
            "total_events": None,
            "percent_complete": 0,
            "elapsed_seconds": 0,
            "estimated_remaining_seconds": None,
            "failure_reason": None,
            "worker_pid": None,
        }
        self._write_status(run_id, status)
        command = [
            sys.executable,
            str(ROOT / "backtest_worker.py"),
            "--job-dir",
            str(directory),
        ]
        try:
            # The child keeps its own descriptors. Close the parent's copies
            # even when launch fails, rather than leaking two handles per job.
            with (directory / "worker.stdout.log").open("ab") as stdout, (directory / "worker.stderr.log").open("ab") as stderr:
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "KURU_SDK_BROKER_URL": broker.url,
                        "KURU_SDK_BROKER_TOKEN": broker.token,
                    },
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                )
        except OSError as error:
            status.update(status="failed", stage="failed", failed_at=utc_now(),
                          failure_reason=f"worker launch failed: {error}")
            self._write_status(run_id, status)
            raise JobError("the backtest worker could not start") from error
        with self._lock:
            self._processes[run_id] = process
        # The worker publishes its PID and progress. Do not write the queued
        # status after launch: a fast worker may already have completed.
        return {"run_id": run_id, "owner_token": owner_token, "status": "queued", "status_url": f"/api/backtests/{run_id}"}

    def authorize(self, run_id: str, owner_token: str | None) -> dict[str, Any]:
        status = self._read_status(run_id)
        supplied = hashlib.sha256((owner_token or "").encode()).hexdigest()
        if not secrets.compare_digest(supplied, str(status["owner_token_sha256"])):
            raise JobError("job owner token is invalid")
        return status

    def status(self, run_id: str, owner_token: str | None) -> dict[str, Any]:
        status = self.authorize(run_id, owner_token)
        current = status["status"]
        with self._lock:
            process = self._processes.get(run_id)
        if current not in TERMINAL_STATES and process is not None and process.poll() is not None:
            status.update({
                "status": "failed",
                "stage": "failed",
                "failed_at": utc_now(),
                "failure_reason": f"worker exited with code {process.returncode} without a terminal status",
            })
            self._write_status(run_id, status)
        elif current == "running" and time.time() - parse_time(status["last_progress_at"]) > STALL_SECONDS:
            status.update({"status": "stalled", "stage": "stalled", "stalled_at": utc_now(), "failure_reason": "no heartbeat or event progress for 120 seconds"})
            self._write_status(run_id, status)
        return {key: value for key, value in status.items() if key != "owner_token_sha256"}

    def result(self, run_id: str, owner_token: str | None) -> dict[str, Any]:
        status = self.authorize(run_id, owner_token)
        if status["status"] != "completed":
            raise JobError(f"job is {status['status']}; no completed result is available")
        result_path = self._directory(run_id) / "result.json"
        complete_path = self._directory(run_id) / "complete.json"
        if not result_path.exists() or not complete_path.exists():
            raise JobError("completed job output is incomplete")
        payload = result_path.read_bytes()
        complete = json.loads(complete_path.read_text(encoding="utf-8"))
        if hashlib.sha256(payload).hexdigest() != complete.get("result_file_sha256"):
            raise JobError("completed job result hash does not match")
        return json.loads(payload)

    def fills(self, run_id: str, owner_token: str | None) -> list[dict[str, Any]]:
        """Return detailed fill records only when the caller asks for them."""
        status = self.authorize(run_id, owner_token)
        if status["status"] != "completed":
            raise JobError(f"job is {status['status']}; no completed fills are available")
        fills_path = self._directory(run_id) / "fills.json"
        complete_path = self._directory(run_id) / "complete.json"
        if not fills_path.exists() or not complete_path.exists():
            raise JobError("completed job fill output is incomplete")
        payload = fills_path.read_bytes()
        complete = json.loads(complete_path.read_text(encoding="utf-8"))
        if hashlib.sha256(payload).hexdigest() != complete.get("fills_file_sha256"):
            raise JobError("completed job fill hash does not match")
        decoded = json.loads(payload)
        if not isinstance(decoded, list):
            raise JobError("completed job fill output is invalid")
        return decoded

    def cancel(self, run_id: str, owner_token: str | None) -> dict[str, Any]:
        status = self.authorize(run_id, owner_token)
        if status["status"] in {"completed", "failed", "canceled"}:
            return {key: value for key, value in status.items() if key != "owner_token_sha256"}
        directory = self._directory(run_id)
        atomic_write_bytes(directory / "cancel.requested", b"cancel\n")
        with self._lock:
            process = self._processes.get(run_id)
        if process is not None and process.poll() is None:
            process.terminate()
        status.update({"status": "canceled", "stage": "canceled", "canceled_at": utc_now(), "last_progress_at": utc_now()})
        self._write_status(run_id, status)
        live_status_path(run_id).unlink(missing_ok=True)
        return {key: value for key, value in status.items() if key != "owner_token_sha256"}

    def record_response_transmission(
        self, run_id: str, owner_token: str | None, metrics: dict[str, Any]
    ) -> None:
        status = self.authorize(run_id, owner_token)
        status["http_response"] = metrics
        self._write_status(run_id, status)

