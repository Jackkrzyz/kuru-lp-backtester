"""Read-only local Trade candle index. No reconstruction or network requests.

One background reader builds resumable per-file minute bars. HTTP snapshots
are bounded; partial candles and incomplete chart coverage remain explicit.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from collections import OrderedDict

from kuru_backtester.engine import l3_vertical_slice as l3
from kuru_backtester.engine.rebuild_from_chain import EVENT_SPECS, MARKET_ADDRESS
from kuru_backtester.history.verify_all_downloads import downloaded_partitions
from kuru_backtester.history.historical_system import HistoricalEventStore, ValidationLayer
from kuru_backtester.history.reconstruction_profiles import CURRENT_IMPLEMENTATION

TRADE_TOPIC = next(event.topic for event in EVENT_SPECS if event.name == "Trade")


def write_cache(path, value):
    """Publish complete JSON with a private temp file for each writer.

    Multiple local servers may share the same deterministic cache key. Their
    writes must not share a temp filename, even across separate processes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def subtract(first, last, covered):
    result = []
    for a, b in sorted(covered):
        if b < first:
            continue
        if a > last:
            break
        if a > first:
            result.append((first, a - 1))
        first = max(first, b + 1)
    if first <= last:
        result.append((first, last))
    return result


def merge_coverage(ranges):
    """Keep coverage bounded by gaps, not by the number of archive files."""
    merged = []
    for first, last in sorted(ranges):
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def candle_start(timestamp, seconds):
    """Weekly candles start on Monday at 00:00 UTC, not epoch Thursday."""
    offset = 345600 if seconds % 604800 == 0 else 0
    return (int(timestamp) - offset) // seconds * seconds + offset


def merge_bars(bars, seconds=60):
    output = {}
    for bar in bars:
        key = candle_start(bar["time"], seconds)
        old = output.get(key)
        if old is None:
            output[key] = {**bar, "time": key}
            continue
        if bar["first_position"] < old["first_position"]:
            old.update(open=bar["open"], first_position=bar["first_position"])
        if bar["last_position"] > old["last_position"]:
            old.update(close=bar["close"], last_position=bar["last_position"])
        old.update(high=max(old["high"], bar["high"]), low=min(old["low"], bar["low"]),
                   volume_raw=old["volume_raw"] + bar["volume_raw"], trades=old["trades"] + bar["trades"])
    return sorted(output.values(), key=lambda bar: bar["time"])


def read_trades(path, ranges, stopped):
    bars = {}
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if stopped.is_set():
                raise InterruptedError("Candle indexing stopped")
            if TRADE_TOPIC not in line:
                continue
            raw = json.loads(line)
            event = l3.decode_event(raw)
            if event.name != "Trade" or not any(a <= event.block_number <= b for a, b in ranges):
                continue
            if str(raw.get("address", "")).lower() != MARKET_ADDRESS:
                raise ValueError("Chart source contains a foreign market")
            stamp = int(raw["block_timestamp"])
            price, size = int(event.args["price"]), int(event.args["filled_size"])
            if price <= 0 or size < 0:
                raise ValueError("Invalid Trade price or size")
            minute = stamp // 60 * 60
            position = [event.block_number, event.transaction_index, event.log_index]
            bar = {"time": minute, "open": price, "high": price, "low": price, "close": price,
                   "first_position": position, "last_position": position, "volume_raw": size, "trades": 1}
            bars[minute] = merge_bars([bars[minute], bar])[0] if minute in bars else bar
    return list(bars.values())


class MarketChart:
    def __init__(self, root, *, ready_only=True, verified_only=False):
        self.root = Path(root).resolve()
        self.ready_only = ready_only and not verified_only
        self.verified_only = verified_only
        self.validation = ValidationLayer()
        self.directory = self.root / (".cache/market_chart_verified" if verified_only else
                                      ".cache/market_chart_ready" if ready_only else ".cache/market_chart")
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = None
        self.next_refresh = 0
        self.bars = []
        self.coverage = []
        self.snapshot_cache = OrderedDict()
        self.snapshot_source = None
        self.has_complete_index = False
        self.indexed_identity = None
        self.indexed_records = set()
        self.source_records = set()
        self.manifest_revision = None
        self.manifest_identity = None
        self.overview = None
        try:
            overview = json.loads((self.directory / "overview.json").read_text())
            if (overview.get("schema_version") == 1 and overview.get("market") == MARKET_ADDRESS
                    and overview.get("candle_seconds") == 86400
                    and isinstance(overview.get("candles"), list)
                    and isinstance(overview.get("indexed_ranges"), list)
                    and isinstance(overview.get("updated_at"), (int, float))
                    and overview.get("source_identity") == self.source_identity()):
                self.overview = overview
                self.overview["indexed_ranges"] = [
                    {"from_block": a, "to_block": b} for a, b in merge_coverage(
                        (row["from_block"], row["to_block"]) for row in overview["indexed_ranges"])]
        except (OSError, ValueError):
            pass
        self.state = {"phase": "idle", "processed_files": 0, "total_files": 0}

    def source_identity(self):
        if self.verified_only:
            # Only small manifest metadata is read on HTTP polls. The reader
            # checks content hashes before it accepts any partition.
            rows = [(path.name, path.stat().st_size, path.stat().st_mtime_ns)
                    for path in sorted(self.root.glob("*manifest.json"))]
            if rows != self.manifest_revision:
                records = set()
                for name, _, _ in rows:
                    saved = json.loads((self.root / name).read_text())
                    if saved.get("market", "").lower() != MARKET_ADDRESS:
                        continue
                    checkpoints = {row["block_number"]: row["sha256"] for row in saved.get("checkpoints", [])}
                    for part in saved.get("event_partitions", []):
                        records.add((part["path"], part["from_block"], part["to_block"], part.get("sha256"),
                                     part.get("validation_summary_sha256"), checkpoints.get(part["from_block"] - 1)))
                self.source_records = records
                self.manifest_revision = rows
                self.manifest_identity = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
            return self.manifest_identity
        if not self.ready_only:
            return "downloaded_trade_archive"
        digest = hashlib.sha256()
        for name in ("historical_index_manifest.json", ".cache/lp_contract_rules.json"):
            path = self.root / name
            digest.update(path.read_bytes() if path.exists() else b"missing")
        return digest.hexdigest()

    def sources(self):
        # Validated index first: usable initial candles appear before a large
        # download archive is inspected. Do not derive OHLC from sampled points.
        manifest = self.root / "historical_index_manifest.json"
        if self.verified_only:
            for manifest_path in sorted(self.root.glob("*manifest.json")):
                saved = json.loads(manifest_path.read_text())
                if saved.get("market", "").lower() != MARKET_ADDRESS or not saved.get("event_partitions"):
                    continue
                store = HistoricalEventStore(manifest_path, self.validation)
                for part in store.partitions:
                    try:
                        summary = self.validation.verify_partition(part, MARKET_ADDRESS)
                        # The original golden reports predate embedded hashes.
                        # Their approved primary manifest binds both file hashes.
                        primary = manifest_path.name == "historical_index_manifest.json"
                        if summary["event_input"].get("source_sha256", part.sha256 if primary else None) != part.sha256:
                            raise ValueError("Validation does not bind the event hash")
                        checkpoint = next(row for row in store.manifest["checkpoints"]
                                          if row["block_number"] == part.from_block - 1)
                        if checkpoint["sha256"] != summary.get("checkpoint_sha256", checkpoint["sha256"] if primary else None):
                            raise ValueError("Validation does not bind the starting checkpoint")
                        self.validation.verify_file(store._path(checkpoint["path"]), checkpoint["sha256"])
                    except (ValueError, RuntimeError, KeyError, StopIteration, OSError) as error:
                        with self.lock:
                            self.state["warning"] = f"Excluded {part.from_block}–{part.to_block}: {error}"
                        continue
                    yield part.path, part.from_block, part.to_block
            return
        if self.ready_only:
            rules_path = self.root / ".cache/lp_contract_rules.json"
            if not manifest.exists() or not rules_path.exists():
                return
            rules = json.loads(rules_path.read_text())
            store = HistoricalEventStore(manifest, self.validation)
            if rules.get("market") != store.market or store.market != MARKET_ADDRESS:
                raise ValueError("Chart market or LP rules do not match")
            for part in store.partitions:
                checkpoint = next((row for row in store.manifest["checkpoints"]
                                   if row["block_number"] == part.from_block - 1), None)
                rule = rules.get("checkpoints", {}).get(str(part.from_block - 1))
                if (not checkpoint or not rule or rule.get("checkpoint_sha256") != checkpoint["sha256"]
                        or rule.get("implementation_code_sha256") != CURRENT_IMPLEMENTATION
                        or int(rule.get("vault_bid_size", 0)) or int(rule.get("vault_ask_size", 0))):
                    continue
                summary = self.validation.verify_partition(part, MARKET_ADDRESS)
                guards = summary.get("contract_guards", [])
                if any(row.get("implementation_code_sha256") != CURRENT_IMPLEMENTATION
                       or row.get("vault_active") is not False for row in guards):
                    continue
                checkpoint_path = store._path(checkpoint["path"])
                self.validation.verify_file(checkpoint_path, checkpoint["sha256"])
                payload = json.loads(checkpoint_path.read_text())
                if any(int(rule["parameters"][key]) != int(value) for key, value in payload["parameters"].items()):
                    raise ValueError("Chart LP parameters do not match the checkpoint")
                yield part.path, part.from_block, part.to_block
            return
        if manifest.exists():
            saved = json.loads(manifest.read_text())
            for item in saved.get("event_partitions", []):
                yield self.root / item["path"], item["from_block"], item["to_block"]
            trade = saved.get("trade_chart_coverage")
            if trade:
                yield self.root / trade["path"], trade["from_block"], trade["to_block"]
        def discovery_progress(**fields):
            with self.lock:
                self.state = {**self.state, "phase": "finding_local_files", "updated_at": time.time()}
        discovery_progress()
        for item in downloaded_partitions(self.root, discovery_progress):
            yield (self.root / item["source"] / f'{item["from_block"]}-{item["to_block"]}' / "events.jsonl",
                   item["from_block"], item["to_block"])

    def build(self):
        covered, candles = [], {}
        # Keep the previous usable chart visible during refresh. Publishing
        # the first partition of every refresh would erase the broad history
        # until the rest of the archive had been inspected again.
        with self.lock:
            publish_partial = not self.coverage
            self.state.pop("error", None)
            self.state.pop("warning", None)
        last_publish = 0.0
        try:
            source_identity = self.source_identity()
            source_records = set(self.source_records)
            for index, (path, first, last) in enumerate(self.sources()):
                if self.stopped.is_set():
                    return
                if not path.resolve().is_relative_to(self.root) or path.is_symlink():
                    raise ValueError("Chart source leaves the project")
                ranges = subtract(first, last, covered)
                if not ranges:
                    continue
                stat = path.stat()
                identity = [1, str(path.relative_to(self.root)), stat.st_size, stat.st_mtime_ns, ranges]
                key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
                cache = self.directory / (key + ".json")
                with self.lock:
                    self.state = {**self.state, "phase": "indexing_local_trades", "current_range": [first, last],
                                  "processed_files": index, "updated_at": time.time()}
                if cache.exists():
                    contribution = json.loads(cache.read_text())
                else:
                    contribution = read_trades(path, ranges, self.stopped)
                    after = path.stat()
                    if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                        raise ValueError("Candle source changed while reading; retry on the next refresh")
                    write_cache(cache, contribution)
                covered = merge_coverage([*covered, *ranges])
                for bar in contribution:
                    minute = bar["time"]
                    candles[minute] = merge_bars([candles[minute], bar])[0] if minute in candles else bar
                publish = publish_partial and time.monotonic() - last_publish >= 1
                bars = sorted(candles.values(), key=lambda bar: bar["time"]) if publish else None
                with self.lock:
                    if publish:
                        self.bars, self.coverage = bars, list(covered)
                        last_publish = time.monotonic()
                    self.state["processed_files"] = index + 1
                    self.state["candles"] = len(candles)
                    self.state["last_indexed_range"] = [first, last]
            with self.lock:
                self.bars = sorted(candles.values(), key=lambda bar: bar["time"])
                self.coverage = list(covered)
                self.state = {**self.state, "phase": "complete", "updated_at": time.time()}
                self.has_complete_index = True
                self.indexed_identity = source_identity
                self.indexed_records = source_records
                self.next_refresh = time.monotonic() + 300
            # A small daily overview opens the chart immediately next time.
            # It is display data only; L3 eligibility remains independently checked.
            if self.source_identity() != source_identity and not self.verified_only:
                raise ValueError("Chart coverage changed during indexing; retry with the updated index")
            if self.verified_only and self.source_identity() != source_identity:
                # A worker can commit the next partition during this scan.
                # Keep this checked subset and pick up the new commit next.
                self.next_refresh = 0
                return
            overview = {**self.snapshot(), "schema_version": 1, "updated_at": time.time(),
                        "source_identity": source_identity}
            overview.pop("state", None)
            write_cache(self.directory / "overview.json", overview)
            self.overview = overview
        except InterruptedError:
            pass
        except Exception as error:
            with self.lock:
                if self.ready_only or self.verified_only:
                    self.bars, self.coverage, self.overview = [], [], None
                self.state = {**self.state, "phase": "failed", "error": str(error), "updated_at": time.time()}
        finally:
            self.next_refresh = time.monotonic() + (300 if self.state["phase"] == "complete" else 60)

    def request_refresh(self):
        """Start local Trade indexing without blocking an HTTP control request."""
        with self.lock:
            if (self.ready_only or self.verified_only) and self.overview and self.overview.get("source_identity") != self.source_identity():
                self.overview = None
                self.next_refresh = 0
            if self.stopped.is_set():
                raise ValueError("The local candle reader is closed")
            if self.thread is not None and self.thread.is_alive():
                return {"phase": self.state["phase"], "already_running": True}
            self.next_refresh = 0
            self.thread = threading.Thread(target=self.build, daemon=True, name="local-trade-candles")
            self.thread.start()
            return {"phase": "indexing_local_trades", "already_running": False}

    def snapshot(self, first=None, last=None, seconds=86400):
        if seconds not in (60, 3600, 86400, 604800):
            raise ValueError("Candle interval must be 60, 3600, 86400, or 604800 seconds")
        if (first is None) != (last is None) or (first is not None and first > last):
            raise ValueError("Supply an ordered from_time and to_time range")
        with self.lock:
            identity = self.source_identity() if self.ready_only or self.verified_only else None
            if (self.ready_only or self.verified_only) and ((self.overview and self.overview.get("source_identity") != identity)
                                    or (self.indexed_identity is not None and self.indexed_identity != identity)):
                self.overview = None
                # Appended verified partitions do not invalidate the already
                # checked chart. Removed or changed records must fail closed.
                if not (self.verified_only and self.indexed_records and self.indexed_records <= self.source_records):
                    self.bars, self.coverage = [], []
                self.has_complete_index = False
                self.indexed_identity = None
                self.next_refresh = 0
            if not self.stopped.is_set() and (self.thread is None or not self.thread.is_alive()) and time.monotonic() >= self.next_refresh:
                self.thread = threading.Thread(target=self.build, daemon=True, name="local-trade-candles")
                self.thread.start()
            bars, coverage, state = self.bars, self.coverage, dict(self.state)
            if self.overview and not self.has_complete_index and seconds >= 86400:
                saved = self.overview
                candles = saved["candles"]
                if first is not None:
                    start, end = candle_start(first, seconds), candle_start(last, seconds) + seconds - 1
                    candles = [bar for bar in candles if start <= bar["time"] <= end]
                if seconds == 604800:
                    # Daily OHLC retains exact Trade extrema and positions.
                    # It can form weeks while the minute index loads locally.
                    candles = [{**bar, "volume_raw": int(bar["volume_raw"])} for bar in candles]
                    candles = [{**bar, "volume_raw": str(bar["volume_raw"]),
                                "volume_mon": bar["volume_raw"] / 10**10,
                                "from_block": bar["first_position"][0], "to_block": bar["last_position"][0]}
                               for bar in merge_bars(candles, seconds)]
                return {"market": MARKET_ADDRESS, "state": state, "candle_seconds": seconds,
                        "backtest_ready_only": self.ready_only,
                        "verified_only": self.verified_only,
                        "candles": candles, "indexed_ranges": saved["indexed_ranges"],
                        "saved_overview": True, "overview_updated_at": saved["updated_at"]}
            if self.snapshot_source is not bars:
                self.snapshot_cache.clear()
                self.snapshot_source = bars
            cache_key = (id(bars), first, last, seconds)
            saved = self.snapshot_cache.get(cache_key)
            if saved is not None and saved[0] is bars:
                self.snapshot_cache.move_to_end(cache_key)
                return {**saved[1], "state": state}
        source_bars = bars
        if first is not None and last is not None:
            # A viewport must not truncate a daily/hourly candle's OHLC.
            first = candle_start(first, seconds)
            last = candle_start(last, seconds) + seconds - 1
            bars = [bar for bar in bars if first <= bar["time"] <= last]
        bars = merge_bars(bars, seconds)
        while len(bars) > 1500:
            seconds *= 2
            bars = merge_bars(bars, seconds)
        result = {"market": MARKET_ADDRESS, "state": state, "candle_seconds": seconds,
                "backtest_ready_only": self.ready_only,
                "verified_only": self.verified_only,
                "indexed_ranges": [{"from_block": a, "to_block": b} for a, b in coverage],
                "candles": [{**bar, **{key: bar[key] / 10**18 for key in ("open", "high", "low", "close")},
                             "volume_mon": bar["volume_raw"] / 10**10,
                             "volume_raw": str(bar["volume_raw"]), "from_block": bar["first_position"][0],
                             "to_block": bar["last_position"][0]} for bar in bars]}
        with self.lock:
            # Repeated status requests reuse prepared candles. Retain the
            # source object so a recycled object ID cannot hit an old entry.
            if self.bars is source_bars:
                self.snapshot_cache[cache_key] = (source_bars, {k: v for k, v in result.items() if k != "state"})
                self.snapshot_cache.move_to_end(cache_key)
                while len(self.snapshot_cache) > 8:
                    self.snapshot_cache.popitem(last=False)
        return result

    def close(self):
        self.stopped.set()
