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

from kuru_backtester.paths import ROOT
from kuru_backtester.engine.rebuild_from_chain import DEPLOYMENT_BLOCK, MARKET_ADDRESS, DEFAULT_RPC_URL, RpcClient
from kuru_backtester.history.index_history import EVENT_TOPICS, SCHEMA_VERSION
from kuru_backtester.history.full_history import atomic_json


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


class DataSuite:
    def __init__(self, root=ROOT, spawn=subprocess.Popen):
        self.root = Path(root)
        self.directory = self.root / ".cache/data_suite"
        self.spawn = spawn
        self.process = None
        self.active_id = None
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

    def environment(self):
        """Read two known keys only. Never execute shell code from .env."""
        result = dict(os.environ)
        path = self.root / ".env"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.removeprefix("export ").partition("=")
                if separator and key.strip() in {"ENVIO_API_TOKEN", "MONAD_RPC_URL"}:
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
                            inventory_scan_seconds=self.inventory_seconds)
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
                jobs.append({"job_id": job_id, "request": request, "running": running, "phase": phase, "progress": progress})
            return {**inventory, "jobs": jobs, "token_available": bool(self.environment().get("ENVIO_API_TOKEN")), "controls_available": True}

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
            if self.process is None or self.process.poll() is not None:
                raise DataSuiteError("No download started by this server is running")
            # The downloader commits page cursors. Resume discards any uncommitted page.
            self.process.terminate()
            return {"job_id": self.active_id, "status": "stopping"}

    def close(self):
        with self._cache_lock:
            self._closed = True
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
