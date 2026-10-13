"""Local archive inventory and bounded download controls. No engine mutations."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid

from kuru_backtester.paths import ROOT
from kuru_backtester.engine.rebuild_from_chain import DEPLOYMENT_BLOCK, MARKET_ADDRESS, DEFAULT_RPC_URL, RpcClient
from kuru_backtester.history.index_history import EVENT_TOPICS, SCHEMA_VERSION
from kuru_backtester.history.full_history import atomic_json
from kuru_backtester.history.verify_download import completed_bounds


class DataSuiteError(ValueError):
    pass


def merge_ranges(rows):
    """Union inclusive ranges; overlapping archives must not inflate coverage."""
    result = []
    for row in sorted(rows, key=lambda item: item["from_block"]):
        first, last = int(row["from_block"]), int(row["to_block"])
        if last < first:
            continue
        if result and first <= result[-1]["to_block"] + 1:
            result[-1]["to_block"] = max(last, result[-1]["to_block"])
        else:
            result.append({"from_block": first, "to_block": last})
    return result


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def missing_ranges(first, last, rows):
    """Return bounded gaps in complete, durable coverage (inclusive blocks)."""
    cursor, gaps = first, []
    for row in merge_ranges(rows):
        a, b = max(first, row["from_block"]), min(last, row["to_block"])
        if a > b:
            continue
        if cursor < a:
            gaps.append((cursor, a - 1))
        cursor = max(cursor, b + 1)
    if cursor <= last:
        gaps.append((cursor, last))
    return gaps


class DataSuite:
    def __init__(self, root=ROOT, spawn=subprocess.Popen):
        self.root = Path(root)
        self.directory = self.root / ".cache/data_suite"
        self.spawn = spawn
        self.process = None
        self.active_id = None
        self.verification_process = None
        self.verification_id = None
        self.verification_is_queue = False
        self.pipeline = None
        self._pipeline_thread = None
        self._pipeline_stop = threading.Event()
        from kuru_backtester.server.market_chart import MarketChart
        self.market_chart = MarketChart(self.root, ready_only=False, verified_only=True)
        self.trade_discovery = None
        self._job_history_cache = []
        self._job_history_at = 0
        self.lock = threading.RLock()
        self.cached = None
        self.cached_at = 0
        self.inventory_updated_at = None
        self.inventory_seconds = None
        self.inventory_error = None
        self._cache_lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._refreshing = False
        self._retry_after = 0
        self._closed = False
        self._inventory_thread = None
        self.inventory_refresh_seconds = 60
        self.inventory_from_saved = False
        self._restore_inventory()
        saved_pipeline = read_json(self.directory / "preparation.json")
        if saved_pipeline:
            self.pipeline = {**saved_pipeline, "running": False}
            if saved_pipeline.get("running"):
                self.pipeline["phase"] = "unmanaged"

    def _restore_inventory(self):
        """Reuse the last completed scan while a new scan runs in the background."""
        path = self.directory / "inventory.json"
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            return
        saved = read_json(path)
        if not isinstance(saved, dict):
            return
        value = saved.get("inventory", {})
        if (saved.get("schema_version") != 1 or saved.get("root") != str(self.root.resolve())
                or not isinstance(value, dict) or value.get("market") != MARKET_ADDRESS
                or not isinstance(saved.get("updated_at"), (int, float))):
            return
        if (not all(isinstance(value.get(key), list) for key in
                    ("downloaded_ranges", "verified_ranges", "trade_only_ranges"))
                or not isinstance(value.get("storage_bytes"), dict)
                or not isinstance(value.get("warnings"), list)
                or not all(isinstance(value.get(key), int) for key in
                           ("total_saved_bytes", "saved_file_count", "free_disk_bytes"))):
            return
        for rows in (value["downloaded_ranges"], value["verified_ranges"], value["trade_only_ranges"]):
            if any(not isinstance(row, dict) or not isinstance(row.get("from_block"), int)
                   or not isinstance(row.get("to_block"), int) or row["from_block"] > row["to_block"]
                   for row in rows):
                return
        self.cached = value
        self.inventory_updated_at = saved["updated_at"]
        self.inventory_from_saved = True

    def environment(self):
        """Read known service and RPC transport keys. Never execute shell code."""
        result = dict(os.environ)
        path = self.root / ".env"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.removeprefix("export ").partition("=")
                if separator and key.strip() in {"ENVIO_API_TOKEN", "MONAD_RPC_URL",
                                                "KURU_RPC_TIMESTAMP_BATCH_SIZE", "KURU_RPC_MIN_REQUEST_INTERVAL"}:
                    parts = shlex.split(value, comments=True)
                    if len(parts) == 1 and not result.get(key.strip()):
                        result[key.strip()] = parts[0]
        return result

    def refresh_head(self):
        """One explicit RPC refresh. Normal status polling never uses the network."""
        rpc = RpcClient(self.environment().get("MONAD_RPC_URL", DEFAULT_RPC_URL), timeout=15)
        if int(rpc.call("eth_chainId", []), 16) != 143:
            raise DataSuiteError("RPC must use Monad Mainnet, chain ID 143")
        block = rpc.call("eth_getBlockByNumber", ["finalized", False])
        if not block:
            raise DataSuiteError("RPC did not return a finalized block")
        value = {"finalized_block": int(block["number"], 16), "checked_at": time.time()}
        atomic_json(self.directory / "network.json", value)
        return value

    def inventory(self):
        """Synchronous scan for diagnostics. HTTP handlers use inventory_snapshot()."""
        with self._scan_lock:
            with self._cache_lock:
                if self.cached is not None and time.monotonic() - self.cached_at < self.inventory_refresh_seconds:
                    return self.cached
            started = time.monotonic()
            value = self._scan_inventory()
            with self._cache_lock:
                self.cached = value
                # A slow scan must not consume the lifetime of its own cache.
                self.cached_at = time.monotonic()
                self.inventory_updated_at = time.time()
                self.inventory_seconds = self.cached_at - started
                self.inventory_error = None
                self.inventory_from_saved = False
                updated_at = self.inventory_updated_at
            # A saved scan makes a server restart inexpensive. It is display
            # metadata only; backtests still verify their indexed content hashes.
            path = self.directory / "inventory.json"
            if not path.is_symlink() and path.resolve().is_relative_to(self.root.resolve()):
                # Multiple app servers may observe the same archive. Give each
                # writer its own temporary name, then replace the saved scan.
                staged = self.directory / f"inventory-{os.getpid()}-{threading.get_ident()}.json"
                try:
                    atomic_json(staged, {"schema_version": 1, "root": str(self.root.resolve()),
                                         "updated_at": updated_at, "inventory": value})
                    staged.replace(path)
                except OSError:
                    value["warnings"].append("Inventory snapshot could not be saved; startup will require a full scan.")
            return value

    def _refresh_inventory(self):
        try:
            self.inventory()
        except Exception as error:
            with self._cache_lock:
                self.inventory_error = str(error)
                self._retry_after = time.monotonic() + self.inventory_refresh_seconds
        finally:
            with self._cache_lock:
                self._refreshing = False

    def inventory_snapshot(self):
        """Return the last scan immediately; allow only one background scan at a time."""
        with self._cache_lock:
            due = self.cached is None or time.monotonic() - self.cached_at >= self.inventory_refresh_seconds
            if due and not self._closed and not self._refreshing and time.monotonic() >= self._retry_after:
                self._refreshing = True
                self._inventory_thread = threading.Thread(target=self._refresh_inventory, daemon=True,
                                                          name="data-suite-inventory")
                self._inventory_thread.start()
            snapshot = dict(self.cached) if self.cached is not None else {
                "market": MARKET_ADDRESS, "deployment_block": DEPLOYMENT_BLOCK,
                "downloaded_ranges": [], "verified_ranges": [], "trade_only_ranges": [],
                "downloaded_blocks": None, "verified_blocks": None, "total_saved_bytes": None,
                "storage_bytes": {}, "saved_file_count": None, "free_disk_bytes": None, "warnings": [],
            }
            snapshot.update(inventory_ready=self.cached is not None, inventory_refreshing=self._refreshing,
                            inventory_updated_at=self.inventory_updated_at, inventory_error=self.inventory_error,
                            inventory_scan_seconds=self.inventory_seconds,
                            inventory_from_saved=self.inventory_from_saved)
        # A head refresh must be visible without waiting for another storage scan.
        network = read_json(self.directory / "network.json")
        known_last = max([DEPLOYMENT_BLOCK] + [r["to_block"] for r in snapshot["downloaded_ranges"] + snapshot["trade_only_ranges"]])
        end = int(network.get("finalized_block", known_last))
        snapshot.update(network=network, scope_to_block=end, scope_is_finalized=bool(network),
                        total_scope_blocks=end - DEPLOYMENT_BLOCK + 1)
        if snapshot["inventory_ready"]:
            for name in ("downloaded", "verified"):
                snapshot[name + "_blocks"] = sum(max(0, min(r["to_block"], end) - max(r["from_block"], DEPLOYMENT_BLOCK) + 1)
                                                 for r in snapshot[name + "_ranges"])
        return snapshot

    def _scan_inventory(self):
        downloaded, verified, trade_only, warnings = [], [], [], []
        # These are metadata checks, not repeated hash verification of large files.
        resolved_root = self.root.resolve()
        for path in sorted(self.root.glob("*manifest.json")):
            manifest = read_json(path)
            if manifest.get("market", "").lower() != MARKET_ADDRESS:
                continue
            def present(name):
                file = (self.root / str(name)).resolve()
                return resolved_root in file.parents and file.is_file()
            checkpoints = [int(item["block_number"]) for item in manifest.get("checkpoints", [])
                           if item.get("sha256") and present(item.get("path"))]
            for item in manifest.get("event_partitions", []):
                if not present(item.get("path")):
                    warnings.append(f"Missing event file in {path.name}")
                    continue
                downloaded.append(item)
                if (item.get("sha256") and item.get("validation_summary_sha256")
                        and present(item.get("validation_summary"))
                        and any(block <= int(item["from_block"]) - 1 for block in checkpoints)):
                    verified.append(item)
            scope = manifest.get("trade_chart_coverage")
            if scope and present(scope.get("path")):
                trade_only.append(scope)
        # Inspect cursor metadata only. Do not load event JSONL files for the page.
        storage = {}
        file_count = 0
        for label in (".cache", "validation_output", "strategy_output", "lp_output", "multi_regime_output", "chain_data", "output"):
            total = 0
            root = self.root / label
            pending = [] if root.is_symlink() else [root]
            while pending:
                try:
                    with os.scandir(pending.pop()) as entries:
                        files = []
                        for entry in entries:
                            if entry.is_symlink():
                                continue
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    pending.append(entry.path)
                                elif entry.is_file(follow_symlinks=False):
                                    files.append((entry.name, Path(entry.path), entry.stat(follow_symlinks=False).st_size))
                            except FileNotFoundError:
                                continue  # Atomic file replacement can race this read-only scan.
                except FileNotFoundError:
                    continue
                for name, file, size in files:
                    if file.parent == self.directory and (name == "inventory.json" or name.startswith("inventory-")):
                        continue  # Exclude the inventory's own snapshot from its totals.
                    total += size
                    file_count += 1
                    if name == "state.json" and label == ".cache":
                        state = read_json(file)
                        signature = state.get("signature", {})
                        if (signature.get("market", "").lower() == MARKET_ADDRESS
                                and signature.get("schema_version") == SCHEMA_VERSION
                                and "address" in signature.get("log_fields", [])
                                and sorted(signature.get("topics", [])) == EVENT_TOPICS
                                and (file.parent / "events.jsonl").exists()):
                            first = int(signature["from_block"])
                            last = min(int(signature["to_block"]), int(state.get("next_block", first)) - 1)
                            if last >= first:
                                downloaded.append({"from_block": first, "to_block": last})
            storage[label] = total
        downloaded, verified, trade_only = map(merge_ranges, (downloaded, verified, trade_only))
        network = read_json(self.directory / "network.json")
        known_last = max([DEPLOYMENT_BLOCK] + [row["to_block"] for row in downloaded + trade_only])
        end = int(network.get("finalized_block", known_last))
        count = lambda rows: sum(max(0, min(row["to_block"], end) - max(row["from_block"], DEPLOYMENT_BLOCK) + 1) for row in rows)
        return {
            "market": MARKET_ADDRESS, "deployment_block": DEPLOYMENT_BLOCK,
            "scope_to_block": end, "network": network,
            "scope_is_finalized": bool(network), "downloaded_ranges": downloaded,
            "verified_ranges": verified, "trade_only_ranges": trade_only,
            "downloaded_blocks": count(downloaded), "verified_blocks": count(verified),
            "total_scope_blocks": end - DEPLOYMENT_BLOCK + 1,
            "storage_bytes": storage, "total_saved_bytes": sum(storage.values()),
            "saved_file_count": file_count, "free_disk_bytes": shutil.disk_usage(self.root).free,
            "warnings": warnings, "coverage_basis": "Recorded validation metadata; hashes are not rechecked on each poll.",
        }

    def status(self):
        inventory = self.inventory_snapshot()
        with self.lock:
            queue = self.verification_queue_status()
            jobs = []
            for path in sorted(self.directory.glob("jobs/*/request.json")):
                request = read_json(path)
                job_id = path.parent.name
                progress = read_json(path.parent / "progress.json")
                phase = progress.get("phase", "not_started")
                running = job_id == self.active_id and self.process is not None and self.process.poll() is None
                if job_id == self.active_id and not running and self.process is not None:
                    if self.process.returncode != 0:
                        phase = "stopped" if self.process.returncode < 0 else "failed"
                        progress["error"] = progress.get("error") or "Download stopped. See the job log. Resume uses the saved cursor."
                elif not running and phase not in {"complete", "failed", "stopped", "idle", "not_started"}:
                    phase = "unmanaged"
                bounds = completed_bounds(request, progress)
                jobs.append({"job_id": job_id, "request": request, "running": running, "phase": phase, "progress": progress,
                             "verification_bounds": list(bounds) if bounds else None})
            verifications = []
            for path in sorted(self.directory.glob("verifications/*/request.json")):
                verification_id = path.parent.name
                progress = read_json(path.parent / "progress.json")
                process = self.verification_process if verification_id == self.verification_id else None
                running = process is not None and process.poll() is None
                if queue and queue["running"] and queue["progress"].get("current_job_id") == verification_id:
                    running = True
                phase = progress.get("phase", "starting" if running else "not_started")
                if process is not None and not running and process.returncode != 0:
                    phase = "stopped" if process.returncode < 0 else "failed"
                    progress["error"] = progress.get("error") or "Verification stopped. See verification.log in the job directory."
                elif not running and phase not in {"complete", "failed", "stopped", "not_started"}:
                    phase = "unmanaged"
                verifications.append({"job_id": verification_id, "request": read_json(path), "running": running,
                                      "phase": phase, "progress": progress})
            # Durable page progress is a small read on every poll. A running
            # download need not wait for the next recursive storage scan.
            downloaded = list(inventory["downloaded_ranges"])
            for job in jobs:
                request, progress = job["request"], job["progress"]
                cursor = progress.get("downloaded_block")
                if (request.get("market") == MARKET_ADDRESS and request.get("download_only") is True
                        and isinstance(cursor, int) and not isinstance(cursor, bool)
                        and request["from_block"] <= cursor <= request["to_block"]):
                    downloaded.append({"from_block": request["from_block"], "to_block": cursor})
            inventory["downloaded_ranges"] = merge_ranges(downloaded)
            # validated_block is committed only after promotion of a passing
            # partition. Do not wait for the next large disk inventory scan.
            verified = list(inventory["verified_ranges"])
            for job in verifications:
                request, progress = job["request"], job["progress"]
                cursor = progress.get("validated_block")
                if (request.get("market") == MARKET_ADDRESS and isinstance(cursor, int) and not isinstance(cursor, bool)
                        and request["from_block"] <= cursor <= request["to_block"]
                        and (job["running"] or progress.get("updated_at", 0) >= (inventory.get("inventory_updated_at") or 0))
                        and progress.get("event_parity_status") == "pass"
                        and progress.get("queue_invariant_failures") == 0):
                    # New workers can continue after an unsupported partition.
                    # Their explicit ranges must preserve those holes.
                    verified.extend(progress.get("verified_ranges", [
                        {"from_block": request["from_block"], "to_block": cursor}]))
            inventory["verified_ranges"] = merge_ranges(verified)
            end = inventory["scope_to_block"]
            if inventory["inventory_ready"]:
                inventory["downloaded_blocks"] = sum(max(0, min(r["to_block"], end) - max(r["from_block"], DEPLOYMENT_BLOCK) + 1)
                                                     for r in inventory["downloaded_ranges"])
                inventory["verified_blocks"] = sum(max(0, min(r["to_block"], end) - max(r["from_block"], DEPLOYMENT_BLOCK) + 1)
                                                   for r in inventory["verified_ranges"])
            index = read_json(self.root / "historical_index_manifest.json")
            backtest_ranges = merge_ranges(index.get("event_partitions", []))
            discovery = dict(self.trade_discovery.state) if self.trade_discovery else None
            with self.market_chart.lock:
                indexed = [{"from_block": a, "to_block": b} for a, b in self.market_chart.coverage]
                if self.market_chart.overview and not indexed:
                    indexed = self.market_chart.overview["indexed_ranges"]
            inventory["trade_only_ranges"] = merge_ranges([*inventory["trade_only_ranges"], *indexed])
            return {**inventory, "backtest_ranges": backtest_ranges, "local_trade_discovery": discovery,
                    "job_history": self.job_history(),
                    "jobs": jobs, "verifications": verifications, "verification_queue": queue,
                    "pipeline": dict(self.pipeline) if self.pipeline else None,
                    "token_available": bool(self.environment().get("ENVIO_API_TOKEN")), "controls_available": True}

    def job_history(self):
        """Small durable job records; no event files or result timelines."""
        if time.monotonic() - self._job_history_at < 30:
            return self._job_history_cache
        rows = []
        for path in sorted(self.directory.glob("verification_queues/*/progress.json")):
            progress = read_json(path)
            phase = progress.get("phase", "not_started")
            rows.append({"job_id": path.parent.name, "type": "verification",
                         "phase": phase if phase in {"complete", "failed", "stopped"} else "unmanaged",
                         "progress": progress, "request": read_json(path.parent / "request.json")})
        for path in sorted((self.root / ".cache/backtest_jobs").glob("*/status.json")):
            saved = read_json(path)
            progress = {key: saved[key] for key in ("processed_events", "total_events", "percent_complete",
                        "elapsed_seconds", "failure_reason") if key in saved}
            progress["updated_at"] = path.stat().st_mtime
            progress["error"] = saved.get("failure_reason")
            phase = saved.get("status", "unknown")
            rows.append({"job_id": path.parent.name, "type": "backtest",
                         "phase": phase if phase in {"completed", "failed", "canceled", "stalled"} else "unmanaged",
                         "progress": progress, "request": read_json(path.parent / "request.json")})
        for path in sorted(self.directory.glob("preparations/*.json")):
            saved = read_json(path)
            rows.append({"job_id": path.stem, "type": "preparation", "progress": saved,
                         "phase": "unmanaged" if saved.get("running") else saved.get("phase", "unknown")})
        self._job_history_cache, self._job_history_at = rows, time.monotonic()
        return rows

    def verification_queue_status(self):
        """Read small worker telemetry only. Queue planning never blocks polling."""
        if self.verification_is_queue:
            directory = self.directory / "verification_queues" / self.verification_id
            process = self.verification_process
        else:
            paths = list(self.directory.glob("verification_queues/*/request.json"))
            if not paths:
                return None
            directory = max(paths, key=lambda path: path.stat().st_mtime).parent
            process = None
        progress = read_json(directory / "progress.json")
        running = process is not None and process.poll() is None
        phase = progress.get("phase", "starting" if running else "not_started")
        if process is not None and not running and process.returncode != 0:
            phase = "stopped" if process.returncode < 0 else "failed"
            progress["error"] = progress.get("error") or "Queue stopped. Start again to check the remaining data. See queue.log for details."
        elif not running and phase not in {"complete", "failed", "stopped", "not_started"}:
            phase = "unmanaged"
        current = progress.get("current_job_id", "")
        detail = {}
        if isinstance(current, str) and len(current) == 16 and all(c in "0123456789abcdef" for c in current):
            detail = read_json(self.directory / "verifications" / current / "progress.json")
        first, last = progress.get("current_from_block"), progress.get("current_to_block")
        size, count = progress.get("current_partition_blocks"), progress.get("current_partition_count")
        cursor = detail.get("validated_block")
        if "completed_partitions" in detail:
            progress["completed_partitions_base"] = progress.get("completed_partitions", 0)
            progress["completed_partitions"] += detail["completed_partitions"]
            progress["blocked_partitions"] = progress.get("blocked_partitions", 0) + detail.get("blocked_partitions", 0)
            progress["processed_partitions"] = progress.get("processed_partitions", 0) + detail.get("processed_partitions", 0)
        elif (all(isinstance(n, int) and not isinstance(n, bool) for n in (first, last, size, count, cursor))
                and size > 0 and count > 0 and first <= cursor <= last):
            # The on-disk queue counter advances after a whole group. Fold in
            # promoted child partitions for live UI progress, without writes.
            child_done = count if cursor == last else min(count, (cursor - first + 1) // size)
            progress["completed_partitions_base"] = progress.get("completed_partitions", 0)
            progress["completed_partitions"] = progress["completed_partitions_base"] + child_done
        return {"job_id": directory.name, "running": running, "phase": phase, "progress": progress,
                "current_progress": detail}

    def progress(self):
        """Expose terminal process failures even if the CLI failed before telemetry."""
        if self.active_id is None:
            paths = list(self.directory.glob("jobs/*/progress.json"))
            if not paths:
                return None
            latest = max(paths, key=lambda path: path.stat().st_mtime)
            return {**read_json(latest), "observed_job_id": latest.parent.name, "managed_by_this_server": False}
        with self.lock:
            directory = self.directory / "jobs" / self.active_id
            value = read_json(directory / "progress.json")
            if not value:
                value = {"phase": "starting", "configuration": read_json(directory / "request.json"), "updated_at": time.time()}
            if self.process is not None and self.process.poll() is not None and self.process.returncode != 0:
                value["phase"] = "stopped" if self.process.returncode < 0 else "failed"
                value["error"] = value.get("error") or "Download stopped. See the job log. Resume uses the saved cursor."
            return value

    def start(self, payload):
        with self.lock:
            self._check_pipeline_owner()
            if self.process is not None and self.process.poll() is None:
                raise DataSuiteError("A download is already running in this server")
            if payload.get("market", MARKET_ADDRESS).lower() != MARKET_ADDRESS:
                raise DataSuiteError("Only the verified MON / USDC market is supported")
            def integer(key):
                value = payload.get(key)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise DataSuiteError(f"{key} must be an integer")
                return value
            first, last = integer("from_block"), integer("to_block")
            head = read_json(self.directory / "network.json").get("finalized_block")
            if head is None:
                raise DataSuiteError("Refresh the finalized block before starting a download")
            if not DEPLOYMENT_BLOCK <= first <= last <= head:
                raise DataSuiteError("Use a range from market deployment through the checked finalized block")
            if payload.get("confirm_download") is not True:
                raise DataSuiteError("Confirm the bounded network download")
            environment = self.environment()
            if not environment.get("ENVIO_API_TOKEN", "").strip():
                raise DataSuiteError("Set ENVIO_API_TOKEN in the server terminal or local .env")
            request = {"market": MARKET_ADDRESS, "from_block": first, "to_block": last, "partition_blocks": 5000, "download_only": True}
            job_id = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:16]
            directory = self.directory / "jobs" / job_id
            directory.mkdir(parents=True, exist_ok=True)
            atomic_json(directory / "request.json", request)
            command = [sys.executable, "-m", "kuru_backtester.history.full_history", "--market", MARKET_ADDRESS,
                       "--from-block", str(first), "--to-block", str(last), "--partition-blocks", "5000",
                       "--job-root", str(directory), "--download-only", "--execute",
                       "--rpc-url", environment.get("MONAD_RPC_URL", DEFAULT_RPC_URL)]
            with (directory / "download.log").open("ab") as log:
                self.process = self.spawn(command, cwd=self.root, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                          **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
            self.active_id = job_id
            with self._cache_lock:
                self.cached_at = 0  # Keep the old totals visible while a refresh runs.
            return {"job_id": job_id, "request": request}

    def stop(self):
        with self.lock:
            self._check_pipeline_owner()
            if self.process is None or self.process.poll() is not None:
                raise DataSuiteError("No download started by this server is running")
            # The downloader commits page cursors. Resume discards any uncommitted page.
            self.process.terminate()
            return {"job_id": self.active_id, "status": "stopping"}

    def start_verification(self, payload):
        """Start an independent RPC-only job over a fixed completed archive range."""
        with self.lock:
            self._check_pipeline_owner()
            if self.verification_process is not None and self.verification_process.poll() is None:
                raise DataSuiteError("Verification is already running in this server")
            source_id = payload.get("source_job_id", "")
            if not isinstance(source_id, str) or len(source_id) != 16 or any(c not in "0123456789abcdef" for c in source_id):
                raise DataSuiteError("Select a saved download job")
            source = self.directory / "jobs" / source_id
            if source.is_symlink() or not source.resolve().is_relative_to((self.directory / "jobs").resolve()):
                raise DataSuiteError("Invalid source job path")
            saved = read_json(source / "request.json")
            if not saved:
                raise DataSuiteError("The selected download job does not exist")
            bounds = completed_bounds(saved, read_json(source / "progress.json"))
            if bounds is None:
                raise DataSuiteError("No complete downloaded partition is available yet")
            first, last = payload.get("from_block", bounds[0]), payload.get("to_block", bounds[1])
            if any(isinstance(n, bool) or not isinstance(n, int) for n in (first, last)):
                raise DataSuiteError("Verification block bounds must be integers")
            size = saved["partition_blocks"]
            if (not bounds[0] <= first <= last <= bounds[1] or (first - bounds[0]) % size
                    or (last != saved["to_block"] and (last + 1 - bounds[0]) % size)):
                raise DataSuiteError("Use whole completed partitions within the displayed bounds")
            request = {"source_job_id": source_id, "market": MARKET_ADDRESS, "from_block": first, "to_block": last}
            job_id = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:16]
            directory = self.directory / "verifications" / job_id
            directory.mkdir(parents=True, exist_ok=True)
            atomic_json(directory / "request.json", request)
            environment = self.environment()
            command = [sys.executable, "-m", "kuru_backtester.history.verify_download", "--source-job", str(source),
                       "--job-root", str(directory), "--manifest", str(self.root / f"full_history_verify_{job_id}_manifest.json"),
                       "--from-block", str(first), "--to-block", str(last),
                       "--rpc-url", environment.get("MONAD_RPC_URL", DEFAULT_RPC_URL)]
            with (directory / "verification.log").open("ab") as log:
                self.verification_process = self.spawn(command, cwd=self.root, env=environment, stdout=log,
                                                      stderr=subprocess.STDOUT,
                                                      **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
            self.verification_id = job_id
            self.verification_is_queue = False
            return {"job_id": job_id, "request": request}

    def start_all_verification(self):
        """Launch one bounded snapshot queue. No scan or network calls in HTTP."""
        with self.lock:
            self._check_pipeline_owner()
            if self.verification_process is not None and self.verification_process.poll() is None:
                raise DataSuiteError("Verification is already running in this server")
            job_id = uuid.uuid4().hex
            directory = self.directory / "verification_queues" / job_id
            request = {"market": MARKET_ADDRESS, "scope": "completed_local_l3", "created_at": time.time()}
            atomic_json(directory / "request.json", request)
            environment = self.environment()
            command = [sys.executable, "-m", "kuru_backtester.history.verify_all_downloads", "--job-root", str(directory),
                       "--rpc-url", environment.get("MONAD_RPC_URL", DEFAULT_RPC_URL)]
            with (directory / "queue.log").open("ab") as log:
                self.verification_process = self.spawn(command, cwd=self.root, env=environment, stdout=log,
                                                      stderr=subprocess.STDOUT,
                                                      **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
            self.verification_id = job_id
            self.verification_is_queue = True
            return {"job_id": job_id, "request": request}

    def start_local_preparation(self):
        """RPC verification and local Trade candles; no HyperSync worker."""
        with self.lock:
            self._check_pipeline_owner()
            if self.verification_process is not None and self.verification_process.poll() is None:
                raise DataSuiteError("Verification is already running in this server")
            chart = self.market_chart.request_refresh()
            if self.trade_discovery is None:
                from kuru_backtester.server.market_chart import MarketChart
                self.trade_discovery = MarketChart(self.root, ready_only=False)
            discovery = self.trade_discovery.request_refresh()
            queue = self.start_all_verification()
            return {**queue, "local_trade_index": chart, "trade_discovery": discovery, "envio_download": False}

    def stop_verification(self):
        with self.lock:
            self._check_pipeline_owner()
            if self.verification_process is None or self.verification_process.poll() is not None:
                raise DataSuiteError("No verification started by this server is running")
            self.verification_process.terminate()
            return {"job_id": self.verification_id, "status": "stopping"}

    def _check_pipeline_owner(self):
        if (self._pipeline_thread is not None and self._pipeline_thread.is_alive()
                and threading.current_thread() is not self._pipeline_thread):
            raise DataSuiteError("The data preparation job owns these workers. Stop it first.")

    def start_preparation(self, payload):
        """Start one bounded coordinator. Planning stays outside HTTP handlers."""
        with self.lock:
            if self._pipeline_thread is not None and self._pipeline_thread.is_alive():
                raise DataSuiteError("Data preparation is already running")
            if any(p is not None and p.poll() is None for p in (self.process, self.verification_process)):
                raise DataSuiteError("Stop the current download or verification before starting data preparation")
            head = read_json(self.directory / "network.json").get("finalized_block")
            first, last = payload.get("from_block"), payload.get("to_block")
            if (any(isinstance(n, bool) or not isinstance(n, int) for n in (first, last))
                    or head is None or not DEPLOYMENT_BLOCK <= first <= last <= head):
                raise DataSuiteError("Check the finalized block and select a bounded range from deployment through that block")
            if payload.get("market", MARKET_ADDRESS).lower() != MARKET_ADDRESS:
                raise DataSuiteError("Only the verified MON / USDC market is supported")
            if payload.get("confirm_download") is not True:
                raise DataSuiteError("Confirm the bounded network download")
            if not self.environment().get("ENVIO_API_TOKEN", "").strip():
                raise DataSuiteError("Set ENVIO_API_TOKEN in the server terminal or local .env")
            self._pipeline_stop.clear()
            self.process = self.verification_process = None
            self.active_id = self.verification_id = None
            self.verification_is_queue = False
            self.pipeline = {"job_id": uuid.uuid4().hex, "running": True, "phase": "planning",
                             "configuration": {"from_block": first, "to_block": last}, "updated_at": time.time()}
            self._pipeline_thread = threading.Thread(target=self._prepare, args=(first, last), daemon=True,
                                                     name="data-preparation")
            self._pipeline_thread.start()
            return dict(self.pipeline)

    def _prepare(self, first, last):
        from kuru_backtester.history.verify_all_downloads import downloaded_partitions, verified_ranges

        def update(**fields):
            with self.lock:
                self.pipeline.update(fields, updated_at=time.time())
                atomic_json(self.directory / "preparation.json", self.pipeline)
                atomic_json(self.directory / "preparations" / (self.pipeline["job_id"] + ".json"), self.pipeline)

        def planning(**fields):
            if self._pipeline_stop.is_set():
                raise InterruptedError("Data preparation stopped")
            update(**fields)

        try:
            # Only complete source states and hash-checked verification records
            # count. Partial pages are resumed by the existing downloader.
            saved = downloaded_partitions(self.root, planning)
            pending = missing_ranges(first, last, saved)
            # Source archives already prove which blocks were downloaded.
            # Hash only verification outputs needed to close remaining gaps;
            # the queue checks all saved validation hashes before skipping work.
            verified = verified_ranges(self.root, [{"from_block": a, "to_block": b} for a, b in pending], planning) if pending else []
            rows = saved + [{"from_block": a, "to_block": b} for a, b in verified]
            gaps = missing_ranges(first, last, rows)
            # Resume an existing partial archive with its original identity.
            # Its committed pages and completed partitions are reused, not
            # downloaded into a new archive with a shifted starting block.
            resume = []
            for path in sorted(self.directory.glob("jobs/*/request.json")):
                job = read_json(path)
                a, b = job.get("from_block"), job.get("to_block")
                if (job.get("market") == MARKET_ADDRESS and job.get("download_only") is True
                        and isinstance(a, int) and isinstance(b, int) and first <= a <= b <= last
                        and any(x <= b and y >= a for x, y in gaps)):
                    own_saved = [row for row in saved if row.get("source") == path.parent.relative_to(self.root).as_posix()]
                    pending = rows + [{"from_block": x, "to_block": y} for x, y in resume]
                    if missing_ranges(a, b, own_saved) != missing_ranges(a, b, pending):
                        # Another archive covers part of this job. Download only
                        # its actual gaps rather than fetching that data twice.
                        continue
                    resume.append((a, b))
                    gaps = missing_ranges(first, last, rows + [
                        {"from_block": x, "to_block": y} for x, y in resume])
            gaps = sorted(resume + gaps)
            update(phase="preparing", missing_ranges=[list(pair) for pair in gaps])
            if self._pipeline_stop.is_set():
                return
            # The first verifier takes a snapshot of the saved backlog before
            # any new download is launched. Subsequent snapshots catch up.
            self.start_all_verification()
            queue_finished_at = None
            while not self._pipeline_stop.is_set():
                downloading = self.process is not None and self.process.poll() is None
                if self.process is not None and not downloading and self.process.returncode != 0:
                    raise RuntimeError("HyperSync download failed. See download.log. Saved cursors remain available.")
                verifying = self.verification_process is not None and self.verification_process.poll() is None
                if self.verification_process is not None and not verifying and self.verification_process.returncode != 0:
                    detail = self.verification_queue_status()["progress"].get("error")
                    raise RuntimeError(detail or "Reconstruction / verification failed. See queue.log. Saved data is retained.")
                if not downloading and gaps:
                    a, b = gaps.pop(0)
                    self.start({"from_block": a, "to_block": b, "confirm_download": True})
                    downloading = True
                if not verifying:
                    queue = self.verification_queue_status()
                    if queue["phase"] != "complete":
                        raise RuntimeError("Verification worker exited without a completed queue")
                    if queue_finished_at is None:
                        queue_finished_at = time.monotonic()
                    # A final snapshot after the last download completes closes
                    # the race with the previous queue's fixed source snapshot.
                    if not downloading and not gaps:
                        if self.pipeline.get("final_queue"):
                            update(phase="complete", running=False)
                            return
                        update(final_queue=True, phase="final_verification")
                        self.start_all_verification()
                        queue_finished_at = None
                    elif time.monotonic() - queue_finished_at >= 30:
                        self.start_all_verification()
                        queue_finished_at = None
                phase = ("final_verification" if self.pipeline.get("final_queue") else
                         "download_and_verify" if self.verification_process.poll() is None else
                         "waiting_for_completed_partition")
                if self.pipeline["phase"] != phase:
                    update(phase=phase)
                self._pipeline_stop.wait(1)
        except Exception as error:
            update(phase="failed", error=str(error), running=False)
        finally:
            # Stop only children owned by this server. Source states remain intact.
            with self.lock:
                for process in (self.process, self.verification_process):
                    if process is not None and process.poll() is None:
                        process.terminate()
            if self._pipeline_stop.is_set():
                update(phase="stopped", running=False)
            with self._cache_lock:
                self.cached_at = 0

    def stop_preparation(self):
        with self.lock:
            if not self.pipeline or not self.pipeline.get("running"):
                raise DataSuiteError("No data preparation job is running")
            self._pipeline_stop.set()
            return {"job_id": self.pipeline["job_id"], "status": "stopping"}

    def close(self):
        self._pipeline_stop.set()
        if self._pipeline_thread is not None:
            self._pipeline_thread.join(timeout=5)
        self.market_chart.close()
        if self.trade_discovery is not None:
            self.trade_discovery.close()
        with self._cache_lock:
            self._closed = True
        if self.verification_process is not None and self.verification_process.poll() is None:
            self.verification_process.terminate()
            try:
                self.verification_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.verification_process.kill()
                self.verification_process.wait()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
