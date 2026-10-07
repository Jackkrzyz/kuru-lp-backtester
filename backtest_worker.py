#!/usr/bin/env python3
"""Run one isolated backtest job and write atomic status and result files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import backtest_service
import kuru_lp
from backtest_jobs import live_status_path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def process_diagnostics() -> dict[str, Any]:
    """Return small process and thread data for live job diagnosis."""
    threads: list[dict[str, Any]] = []
    task_root = Path("/proc/self/task")
    if task_root.exists():
        ticks = os.sysconf("SC_CLK_TCK")
        for task in sorted(task_root.iterdir(), key=lambda item: int(item.name)):
            try:
                tail = (task / "stat").read_text().split(") ", 1)[1].split()
                threads.append({
                    "thread_id": int(task.name),
                    "state": tail[0],
                    "cpu_seconds": (int(tail[11]) + int(tail[12])) / ticks,
                })
            except (FileNotFoundError, IndexError, ValueError):
                continue
    else:  # Windows Python does not expose Linux /proc thread states.
        threads = [{"thread_id": item.ident, "state": "unknown", "cpu_seconds": None}
                   for item in threading.enumerate()]
    return {
        "process_id": os.getpid(),
        "thread_count": len(threads),
        "threads": threads,
        "active_backend_locks": {
            "global_run_lock": False,
            "job_manager_registry_lock": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-dir", type=Path, required=True)
    args = parser.parse_args()
    job_dir = args.job_dir.resolve()
    status_path = job_dir / "status.json"
    heartbeat_path = live_status_path(job_dir.name)
    request_path = job_dir / "request.json"
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    status = json.loads(status_path.read_text(encoding="utf-8"))

    def write_status(**updates: Any) -> None:
        status.update(updates)
        status["last_progress_at"] = utc_now()
        status["elapsed_seconds"] = time.perf_counter() - started_wall
        status["cpu_seconds"] = time.process_time() - started_cpu
        status["waiting_seconds"] = max(0.0, status["elapsed_seconds"] - status["cpu_seconds"])
        status["process_diagnostics"] = process_diagnostics()
        payload = backtest_service.canonical_bytes(status) + b"\n"
        if status["status"] in {"completed", "failed", "canceled", "stalled"}:
            # Terminal state is a durable recovery record. Running heartbeat
            # data is temporary. Atomic rename on a Windows-mounted WSL path
            # can take seconds, so a heartbeat uses one small direct write.
            backtest_service.atomic_write_bytes(status_path, payload, durable=True)
            heartbeat_path.unlink(missing_ok=True)
        else:
            heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            heartbeat_path.write_bytes(payload)

    def progress(update: dict[str, Any]) -> None:
        processed = int(update.get("processed_events", 0))
        total = int(update.get("total_events", 0))
        elapsed = max(0.000001, time.perf_counter() - started_wall)
        remaining = None
        if processed > 0 and total > processed:
            remaining = (total - processed) / (processed / elapsed)
        write_status(
            status="running",
            stage=str(update.get("stage", "simulation")),
            processed_events=processed,
            total_events=total,
            percent_complete=(100 * processed / total if total else 0),
            estimated_remaining_seconds=remaining,
            active_simulated_orders=update.get("active_simulated_orders"),
            operation_counters=update.get("counters", {}),
        )

    try:
        write_status(status="running", stage="loading_historical_events", job_started_at=utc_now(), worker_pid=os.getpid())
        request = json.loads(request_path.read_text(encoding="utf-8"))
        service = backtest_service.BacktestService()
        result = service.run(
            request,
            progress_callback=progress,
            should_cancel=lambda: (job_dir / "cancel.requested").exists(),
        )
        write_status(status="running", stage="writing_result", processed_events=status.get("total_events") or status.get("processed_events"))
        serialization_started = time.perf_counter()
        result_bytes = backtest_service.canonical_bytes(result) + b"\n"
        serialization_seconds = time.perf_counter() - serialization_started
        disk_started = time.perf_counter()
        backtest_service.atomic_write_bytes(job_dir / "result.json", result_bytes)
        fills_source = (
            backtest_service.RUN_ROOT / str(result["run_id"]) / "fills.json"
        )
        fills_bytes = fills_source.read_bytes()
        expected_fills_hash = str(result["detailed_fills"]["sha256"])
        if hashlib.sha256(fills_bytes.rstrip(b"\n")).hexdigest() != expected_fills_hash:
            raise RuntimeError("cached detailed fills do not match the result hash")
        backtest_service.atomic_write_bytes(job_dir / "fills.json", fills_bytes)
        complete = {
            "schema_version": "2.0",
            "result_file_sha256": hashlib.sha256(result_bytes).hexdigest(),
            "fills_file_sha256": hashlib.sha256(fills_bytes).hexdigest(),
            "completed_at": utc_now(),
        }
        backtest_service.atomic_write_bytes(job_dir / "complete.json", backtest_service.canonical_bytes(complete) + b"\n")
        disk_write_seconds = time.perf_counter() - disk_started
        write_status(
            status="completed", stage="completed", percent_complete=100,
            completed_at=complete["completed_at"], result_run_id=result["run_id"],
            result_serialization_seconds=serialization_seconds,
            result_disk_write_seconds=disk_write_seconds,
            result_serialized_bytes=len(result_bytes),
        )
        return 0
    except kuru_lp.BacktestCancelled as error:
        write_status(status="canceled", stage="canceled", canceled_at=utc_now(), failure_reason=str(error))
        return 2
    except backtest_service.BacktestInputError as error:
        write_status(
            status="failed",
            stage="failed",
            failed_at=utc_now(),
            failure_reason=str(error),
            failure=error.payload(),
        )
        return 1
    except Exception as error:
        write_status(status="failed", stage="failed", failed_at=utc_now(), failure_reason=f"{type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
