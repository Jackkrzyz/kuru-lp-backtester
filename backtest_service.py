#!/usr/bin/env python3
"""Run Kuru LP backtests from local, validated interval data."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import compact_replays
import historical_system
import kuru_lp
import l3_vertical_slice as l3
import strategy_vertical_slice as strategy
from rebuild_from_chain import MARKET_ADDRESS, RpcClient, market_parameters


ROOT = Path(__file__).resolve().parent
INTERVAL_MANIFEST = ROOT / "interval_manifest.json"
CACHE_ROOT = ROOT / ".cache" / "backtest_intervals"
RUN_ROOT = ROOT / ".cache" / "backtest_runs"
SDK_GRID_CACHE_ROOT = ROOT / ".cache" / "sdk_grids"
DECODED_EVENT_HOT_CACHE_ROOT = Path(tempfile.gettempdir()) / "kuru_decoded_events"
ENGINE_VERSION = "kuru-lp-local-v14"
CACHE_SCHEMA_VERSION = "1.0"
REPLAY_SCHEMA_VERSION = "1.2"
PRICE_DECIMALS = 8
BASE_DECIMALS = 18
QUOTE_DECIMALS = 6
MAX_COMPACT_FRAMES = 1_500
SEMANTIC_TIMELINE_SCHEMA_VERSION = "1.0"


class BacktestInputError(ValueError):
    """Report an invalid request with data that the UI can show."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_backtest_input",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "details": self.details}


@dataclass(frozen=True)
class IntervalData:
    name: str
    validation: dict[str, Any]
    book: l3.L3Book
    events: Any
    timestamps: dict[int, int]
    parameters: dict[str, int]
    checkpoint_timestamp: int
    source_sha256: str
    event_file_sha256: str
    checkpoint_sha256: str
    start_market: dict[str, int] | None = None
    generalized_range: bool = False
    checkpoint_block: int | None = None
    golden_fixture: str | None = None
    reconstructed_start: historical_system.ReconstructedStart | None = None


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Hash one file without making a second full in-memory copy."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def decoded_event_cache_path(event_file_sha256: str) -> Path:
    """Return a versioned native-file-system cache path for decoded events."""
    identity = {
        "schema_version": strategy.DECODED_EVENT_CACHE_SCHEMA_VERSION,
        "event_file_sha256": event_file_sha256,
        "l3_decoder_sha256": sha256_file(ROOT / "l3_vertical_slice.py"),
        "event_decoder_sha256": sha256_file(ROOT / "rebuild_from_chain.py"),
        "loader_sha256": sha256_file(ROOT / "strategy_vertical_slice.py"),
    }
    return DECODED_EVENT_HOT_CACHE_ROOT / f"{sha256_value(identity)}.sqlite3"


def semantic_timeline_sha256(records: list[dict[str, Any]]) -> str:
    """Hash ordered strategy and financial meaning, not replay storage form."""
    digest = hashlib.sha256()
    digest.update(f"kuru-semantic-timeline:{SEMANTIC_TIMELINE_SCHEMA_VERSION}\n".encode())
    for record in records:
        projection = {
            "sequence": record["sequence"],
            "record_type": record["record_type"],
            "block_number": record["block_number"],
            "transaction_index": record["transaction_index"],
            "log_index": record["log_index"],
            "market": record["market"],
            "market_event": record.get("market_event"),
            "placements": record.get("placements", []),
            "activations": record.get("activations", []),
            "cancellations": record.get("cancellations", []),
            "fills": record.get("fills", []),
            "order_snapshot_complete": record.get("order_snapshot_complete", False),
            "active_simulated_orders": record.get("active_simulated_orders", []),
            "simulated_order_deltas": record.get("simulated_order_deltas", []),
            "cash": record["cash"],
            "inventory": record["inventory"],
            "reserved_cash": record["reserved_cash"],
            "reserved_inventory": record["reserved_inventory"],
            "marked_portfolio_value": record["marked_portfolio_value"],
            "cumulative_pnl": record["cumulative_pnl"],
            "hold_baseline_pnl": record["hold_baseline_pnl"],
            "cumulative_fees": record["cumulative_fees"],
        }
        digest.update(canonical_bytes(projection))
        digest.update(b"\n")
    return digest.hexdigest()


def atomic_write_bytes(path: Path, payload: bytes, *, durable: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    with temporary.open("wb") as output:
        output.write(payload)
        output.flush()
        if durable:
            os.fsync(output.fileno())
    temporary.replace(path)


def decimal_text(value: Any, decimals: int, field: str, *, positive: bool = True) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise BacktestInputError(f"{field} must be a decimal number") from error
    if not number.is_finite() or (positive and number <= 0) or (not positive and number < 0):
        rule = "positive" if positive else "zero or more"
        raise BacktestInputError(f"{field} must be {rule}")
    quantum = Decimal(1).scaleb(-decimals)
    try:
        normalized = number.quantize(quantum)
    except InvalidOperation as error:
        raise BacktestInputError(f"{field} is outside the supported range") from error
    if normalized != number:
        raise BacktestInputError(f"{field} has more than {decimals} decimal places")
    text = format(normalized, "f").rstrip("0").rstrip(".")
    return text or "0"


def raw_amount(text: str, decimals: int) -> int:
    return int(Decimal(text) * (Decimal(10) ** decimals))


def floor_to_tick(value_raw: int, tick_size: int) -> int:
    """Round a positive raw price down to a market tick."""
    if tick_size <= 0:
        raise ValueError("tick size must be positive")
    return max(tick_size, value_raw // tick_size * tick_size)


def ceil_to_tick(value_raw: int, tick_size: int) -> int:
    """Round a positive raw price up to a market tick."""
    if tick_size <= 0:
        raise ValueError("tick size must be positive")
    return max(tick_size, ((value_raw + tick_size - 1) // tick_size) * tick_size)


def percentage_price_bounds(
    reference_price_raw: int,
    lower_percent: str,
    upper_percent: str,
    tick_size: int,
) -> tuple[int, int]:
    """Convert percentage bounds to outward-rounded raw market prices."""
    lower = Decimal(lower_percent)
    upper = Decimal(upper_percent)
    if lower < 0 or lower >= 100 or upper < 0:
        raise BacktestInputError(
            "lower range must be from 0 to less than 100 percent, and upper range must be zero or more",
            code="invalid_percentage_range",
        )
    scale = Decimal(100)
    minimum_unrounded = int(
        (Decimal(reference_price_raw) * (scale - lower) / scale).to_integral_value(rounding="ROUND_FLOOR")
    )
    maximum_unrounded = int(
        (Decimal(reference_price_raw) * (scale + upper) / scale).to_integral_value(rounding="ROUND_CEILING")
    )
    return floor_to_tick(minimum_unrounded, tick_size), ceil_to_tick(maximum_unrounded, tick_size)


def raw_price_text(value_raw: int, price_precision: int) -> str:
    text = format(Decimal(value_raw) / Decimal(price_precision), "f").rstrip("0").rstrip(".")
    return text or "0"


def validate_sdk_grid(grid: dict[str, Any], min_size_raw: int) -> dict[str, Any]:
    """Validate every final integer order against the market minimum."""
    orders = list(grid.get("orders", []))
    invalid = [
        {
            "side": str(item["side"]),
            "price_raw": str(item["price_raw"]),
            "size_raw": str(item["size_raw"]),
        }
        for item in orders
        if int(item["size_raw"]) < min_size_raw
    ]
    smallest = min((int(item["size_raw"]) for item in orders), default=0)
    return {
        "deployable": bool(orders) and not invalid,
        "smallest_order_size_raw": str(smallest),
        "market_min_size_raw": str(min_size_raw),
        "invalid_order_count": len(invalid),
        "offending_order": invalid[0] if invalid else None,
        "invalid_orders": invalid,
    }


def serialize_book(book: l3.L3Book) -> dict[str, Any]:
    return {
        "orders": [asdict(book.orders[key]) for key in sorted(book.orders)],
        "levels": [
            {
                "is_buy": is_buy,
                "price": price,
                "head": level.head,
                "tail": level.tail,
                "total_size": level.total_size,
            }
            for (is_buy, price), level in sorted(
                book.levels.items(), key=lambda item: (not item[0][0], item[0][1])
            )
        ],
    }


def deserialize_book(payload: dict[str, Any]) -> l3.L3Book:
    book = l3.L3Book()
    book.orders = {
        int(item["order_id"]): l3.BookOrder(
            order_id=int(item["order_id"]), owner=str(item["owner"]), size=int(item["size"]),
            prev=int(item["prev"]), next=int(item["next"]), flipped_id=int(item["flipped_id"]),
            price=int(item["price"]), flipped_price=int(item["flipped_price"]),
            is_buy=bool(item["is_buy"]),
        )
        for item in payload["orders"]
    }
    book.levels = {
        (bool(item["is_buy"]), int(item["price"])): l3.PriceQueue(
            head=int(item["head"]), tail=int(item["tail"]), total_size=int(item["total_size"])
        )
        for item in payload["levels"]
    }
    book.check_invariants()
    return book


def validation_summary_path(interval: str) -> Path:
    return ROOT / "validation_output" / interval / "summary.json"


def checkpoint_cache_path(interval: str) -> Path:
    return CACHE_ROOT / interval / "checkpoint.json"


def prepare_interval_cache(interval: str, rpc_url: str = l3.DEFAULT_RPC_URL) -> Path:
    manifest = json.loads(INTERVAL_MANIFEST.read_text(encoding="utf-8"))
    if interval not in manifest["intervals"]:
        raise BacktestInputError(f"unknown interval: {interval}")
    summary_path = validation_summary_path(interval)
    validation = strategy.verify_validation_summary(summary_path)
    checkpoint_block = int(validation["interval"]["from_block"]) - 1
    rpc = RpcClient(rpc_url, timeout=180)
    book, _, _ = l3.load_checkpoint(rpc, MARKET_ADDRESS, checkpoint_block)
    params = market_parameters(rpc, MARKET_ADDRESS)
    timestamp = rpc.block_timestamps([checkpoint_block])[checkpoint_block]
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "market": MARKET_ADDRESS,
        "interval": interval,
        "checkpoint_block": checkpoint_block,
        "checkpoint_timestamp": timestamp,
        "validation_summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
        "parameters": {
            "price_precision": int(params["price_precision"]),
            "size_precision": int(params["size_precision"]),
            "tick_size": int(params["tick_size"]),
            "min_size": int(params["min_size"]),
            "base_decimals": BASE_DECIMALS,
            "quote_decimals": QUOTE_DECIMALS,
        },
        "book": serialize_book(book),
    }
    path = checkpoint_cache_path(interval)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(payload) + b"\n")
    return path


def resolve_node() -> str:
    explicit = os.environ.get("KURU_NODE")
    if explicit:
        return explicit
    found = shutil.which("node")
    if found:
        return found
    raise RuntimeError("Node.js is required to run the canonical Kuru SDK adapter")


def canonical_sdk_grid(sdk_input: dict[str, Any]) -> dict[str, Any]:
    adapter_path = ROOT / "sdk_reference" / "browser_adapter.js"
    identity = {
        "sdk_input": sdk_input,
        "adapter_sha256": hashlib.sha256(adapter_path.read_bytes()).hexdigest(),
        "package_lock_sha256": hashlib.sha256(
            (ROOT / "sdk_reference" / "package-lock.json").read_bytes()
        ).hexdigest(),
    }
    cache_path = SDK_GRID_CACHE_ROOT / f"{sha256_value(identity)}.json"
    broker_url = os.environ.get("KURU_SDK_BROKER_URL")
    broker_token = os.environ.get("KURU_SDK_BROKER_TOKEN")
    if broker_url and broker_token:
        request = Request(
            broker_url,
            data=canonical_bytes(sdk_input),
            headers={
                "Authorization": f"Bearer {broker_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=35) as response:
                payload = json.loads(response.read())
            grid = payload.get("grid")
            if not isinstance(grid, dict):
                raise RuntimeError(str(payload.get("error", "the SDK broker returned no grid")))
            return grid
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
            # A broker failure must not make a local backtest impossible. The
            # one-shot path below is slower, but it uses the same adapter.
            pass
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    command = [resolve_node(), str(ROOT / "sdk_reference" / "generate_grid_cli.js")]
    completed = subprocess.run(
        command,
        input=json.dumps(sdk_input),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or "The Kuru SDK adapter failed."
        raise BacktestInputError(message)
    grid = json.loads(completed.stdout)
    SDK_GRID_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_bytes(canonical_bytes(grid) + b"\n")
    temporary.replace(cache_path)
    return grid


def normalize_grid(grid: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": str(grid["schema_version"]),
        "source": dict(grid["source"]),
        "shape": str(grid["shape"]),
        "driver": str(grid["driver"]),
        "fee_percent": str(grid["fee_percent"]),
        "fee_pps": str(grid["fee_pps"]),
        "current_market_price_raw": str(grid["current_market_price_raw"]),
        "price_precision": str(grid["price_precision"]),
        "size_precision": str(grid["size_precision"]),
        "quote_asset_decimals": str(grid["quote_asset_decimals"]),
        "base_asset_decimals": str(grid["base_asset_decimals"]),
        "minimum_price_raw": str(grid["minimum_price_raw"]),
        "maximum_price_raw": str(grid["maximum_price_raw"]),
        "min_size_error": bool(grid["min_size_error"]),
        "required_mon_raw": str(grid["required_mon_raw"]),
        "required_usdc_raw": str(grid["required_usdc_raw"]),
        "orders": [
            {
                "side": str(item["side"]),
                "price_raw": str(item["price_raw"]),
                "flip_price_raw": str(item["flip_price_raw"]),
                "size_raw": str(item["size_raw"]),
                "quote_notional_raw": str(item["quote_notional_raw"]),
            }
            for item in grid["orders"]
        ],
    }


class BacktestService:
    def __init__(self) -> None:
        self._intervals: dict[str, IntervalData] = {}
        self._start_markets: dict[str, dict[str, int]] = {}
        self._interval_lock = threading.Lock()
        self._lock = nullcontext()
        self._manifest = json.loads(INTERVAL_MANIFEST.read_text(encoding="utf-8"))
        self._history_validation = historical_system.ValidationLayer()
        self._event_store = historical_system.HistoricalEventStore(
            validation=self._history_validation
        )
        self._checkpoint_store = historical_system.CheckpointStore(
            validation=self._history_validation
        )
        self._reconstruction = historical_system.ReconstructionEngine(
            self._event_store, self._checkpoint_store
        )
        self._lp_range_engine = historical_system.KuruLpBacktestEngine(
            MAX_COMPACT_FRAMES
        )

    def load_interval(self, name: str, metrics: dict[str, float] | None = None) -> IntervalData:
        if name in self._intervals:
            return self._intervals[name]
        with self._interval_lock:
            if name in self._intervals:
                return self._intervals[name]
            if name not in self._manifest["intervals"]:
                raise BacktestInputError(f"unknown interval: {name}")
            cache_path = checkpoint_cache_path(name)
            if not cache_path.exists():
                raise RuntimeError(
                    f"The local checkpoint cache for {name} is missing. "
                    f"Run: python backtest_service.py --prepare-interval {name}"
                )
            cache_bytes = cache_path.read_bytes()
            cache = json.loads(cache_bytes)
            summary_path = validation_summary_path(name)
            summary_sha = hashlib.sha256(summary_path.read_bytes()).hexdigest()
            if cache.get("schema_version") != CACHE_SCHEMA_VERSION or cache.get("validation_summary_sha256") != summary_sha:
                raise RuntimeError(f"The local checkpoint cache for {name} is stale.")
            validation = strategy.verify_validation_summary(summary_path)
            event_relative = validation.get("event_input", {}).get("path")
            if not event_relative:
                raise RuntimeError(f"The validated event file for {name} is missing.")
            validation_root = summary_path.parent.resolve()
            event_path = (validation_root / str(event_relative)).resolve()
            if event_path.parent != validation_root or not event_path.is_file():
                raise RuntimeError("the validated event path must stay inside its interval directory")
            event_sha = sha256_file(event_path)
            loaded = strategy.load_verified_source_events(
                summary_path,
                validation,
                metrics,
                source_sha256=event_sha,
                decoded_cache_path=decoded_event_cache_path(event_sha),
            )
            if loaded is None:
                raise RuntimeError(f"The validated event file for {name} is missing.")
            events, timestamps = loaded
            checkpoint = int(cache["checkpoint_block"])
            checkpoint_timestamp = int(cache["checkpoint_timestamp"])
            timestamps[checkpoint] = checkpoint_timestamp
            data = IntervalData(
                name=name,
                validation=validation,
                book=deserialize_book(cache["book"]),
                events=events,
                timestamps=timestamps,
                parameters={key: int(value) for key, value in cache["parameters"].items()},
                checkpoint_timestamp=checkpoint_timestamp,
                source_sha256=summary_sha,
                event_file_sha256=event_sha,
                checkpoint_sha256=hashlib.sha256(cache_bytes).hexdigest(),
            )
            self._intervals[name] = data
            return data

    def load_range(
        self,
        market: str,
        from_block: int,
        to_block: int,
        metrics: dict[str, float] | None = None,
    ) -> IntervalData:
        """Load one arbitrary, continuously indexed range from local files."""
        if from_block > to_block:
            raise BacktestInputError("from_block must not exceed to_block")
        try:
            self._event_store.require_coverage(market, from_block, to_block)
            reconstructed = self._reconstruction.reconstruct_start(market, from_block)
            cursor = historical_system.ReplayCursor(
                self._event_store, market, from_block, to_block
            )
            timestamps = cursor.timestamps()
            timestamps[from_block - 1] = reconstructed.state_timestamp
            event_count = len(cursor)
        except historical_system.HistoricalRangeError as error:
            raise BacktestInputError(
                str(error), code="historical_range_unavailable", details=error.details
            ) from error
        if event_count == 0:
            raise BacktestInputError("the requested local range contains no market events")

        # The range reference is the reconstructed midpoint after all events in
        # its first block. This is the same rule used by the golden intervals.
        reference_book = historical_system.deserialize_book(
            serialize_book(reconstructed.book)
        )
        for event in cursor:
            if event.block_number != from_block:
                break
            l3.apply_event(
                reference_book, event, reconstructed.parameters["price_precision"]
            )
        reference_book.check_invariants()
        best_bid, best_ask = reference_book.best_prices()
        if best_bid is None or best_ask is None:
            raise BacktestInputError(
                "the reconstructed book at the requested first block has an empty side"
            )
        start_market = {
            "reference_price_raw": (best_bid + best_ask) // 2,
            "best_bid_price_raw": best_bid,
            "best_ask_price_raw": best_ask,
        }
        partition_identity = self._event_store.content_identity(from_block, to_block)
        selected_partitions = self._event_store.selected_partitions(from_block, to_block)
        golden_fixture = None
        if (
            len(selected_partitions) == 1
            and selected_partitions[0].from_block == from_block
            and selected_partitions[0].to_block == to_block
        ):
            golden_fixture = selected_partitions[0].golden_fixture
        event_identity = sha256_value({
            "market": market.lower(),
            "from_block": from_block,
            "to_block": to_block,
            "partitions": partition_identity,
        })
        validation_identity = sha256_value([
            item["validation_summary_sha256"] for item in partition_identity
        ])
        if metrics is not None:
            metrics["indexed_event_count"] = float(event_count)
            metrics["checkpoint_replay_event_count"] = float(
                reconstructed.applied_event_count
            )
        return IntervalData(
            name=f"blocks_{from_block}_{to_block}",
            validation={
                "interval": {
                    "market": market.lower(),
                    "from_block": from_block,
                    "to_block": to_block,
                },
                "event_source": "local_validated_historical_index",
                "event_parity": {
                    "status": "pass",
                    "rpc_event_count": event_count,
                },
            },
            book=reconstructed.book,
            events=cursor,
            timestamps=timestamps,
            parameters=reconstructed.parameters,
            checkpoint_timestamp=reconstructed.state_timestamp,
            source_sha256=validation_identity,
            event_file_sha256=event_identity,
            checkpoint_sha256=reconstructed.checkpoint.sha256,
            start_market=start_market,
            generalized_range=True,
            checkpoint_block=reconstructed.checkpoint.block_number,
            golden_fixture=golden_fixture,
            reconstructed_start=reconstructed,
        )

    def _start_market_from_source(
        self,
        name: str,
        cache: dict[str, Any],
        summary_path: Path,
    ) -> dict[str, int]:
        """Reconstruct only the first interval block for UI reference data."""
        if name in self._start_markets:
            return self._start_markets[name]
        validation = strategy.verify_validation_summary(summary_path)
        from_block = int(validation["interval"]["from_block"])
        event_relative = validation.get("event_input", {}).get("path")
        if not event_relative:
            raise RuntimeError(f"The validated event file for {name} is missing.")
        validation_root = summary_path.parent.resolve()
        event_path = (validation_root / str(event_relative)).resolve()
        if event_path.parent != validation_root or not event_path.is_file():
            raise RuntimeError("the validated event path must stay inside its interval directory")
        book = deserialize_book(cache["book"])
        price_precision = int(cache["parameters"]["price_precision"])
        with event_path.open(encoding="utf-8") as source:
            for line in source:
                payload = json.loads(line)
                block_number = int(payload["block_number"])
                if block_number > from_block:
                    break
                if block_number == from_block:
                    l3.apply_event(book, l3.decode_event(payload), price_precision)
        book.check_invariants()
        best_bid, best_ask = book.best_prices()
        if best_bid is None or best_ask is None:
            raise RuntimeError(f"The reconstructed book at the first block of {name} has an empty side.")
        value = {
            "reference_price_raw": (best_bid + best_ask) // 2,
            "best_bid_price_raw": best_bid,
            "best_ask_price_raw": best_ask,
        }
        validation_rows_path = summary_path.parent / "validation_rows.json"
        if validation_rows_path.is_file():
            rows = json.loads(validation_rows_path.read_text(encoding="utf-8"))
            first = next((item for item in rows if int(item["block_number"]) == from_block), None)
            if first is not None:
                checked_raw = int(Decimal(str(first["reconstructed_midpoint"])) * price_precision)
                if checked_raw != value["reference_price_raw"]:
                    raise RuntimeError("the first-block reference midpoint does not match the validated report")
        self._start_markets[name] = value
        return value

    def interval_metadata(self) -> dict[str, Any]:
        output = []
        for name in self._manifest["intervals"]:
            cache_path = checkpoint_cache_path(name)
            if not cache_path.exists():
                raise RuntimeError(f"The local checkpoint cache for {name} is missing.")
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            summary_path = validation_summary_path(name)
            summary_sha = hashlib.sha256(summary_path.read_bytes()).hexdigest()
            if cache.get("schema_version") != CACHE_SCHEMA_VERSION or cache.get("validation_summary_sha256") != summary_sha:
                raise RuntimeError(f"The local checkpoint cache for {name} is stale.")
            interval = json.loads(summary_path.read_text(encoding="utf-8"))["interval"]
            parameters = {key: int(value) for key, value in cache["parameters"].items()}
            start_market = self._start_market_from_source(name, cache, summary_path)
            output.append({
                "name": name,
                "from_block": int(interval["from_block"]),
                "to_block": int(interval["to_block"]),
                "checkpoint_block": int(interval["from_block"]) - 1,
                **start_market,
                "reference_price": raw_price_text(
                    start_market["reference_price_raw"], parameters["price_precision"]
                ),
                "price_precision": parameters["price_precision"],
                "size_precision": parameters["size_precision"],
                "tick_size": parameters["tick_size"],
                "min_size": parameters["min_size"],
            })
        return {
            "market": MARKET_ADDRESS,
            "engine_version": ENGINE_VERSION,
            "intervals": output,
            "validated_l3_coverage": self._event_store.coverage(),
            "trade_chart_coverage": self._event_store.manifest.get("trade_chart_coverage"),
        }

    def range_metadata(
        self, market: str, from_block: int, to_block: int
    ) -> dict[str, Any]:
        """Return local reconstruction data needed to configure one range."""
        data = self.load_range(market, from_block, to_block)
        start = data.start_market or {}
        return {
            "market": market.lower(),
            "from_block": from_block,
            "to_block": to_block,
            "event_count": len(data.events),
            "checkpoint_block": data.checkpoint_block,
            **start,
            "reference_price": raw_price_text(
                start["reference_price_raw"], data.parameters["price_precision"]
            ),
            **data.parameters,
        }

    def normalize_request(self, request: dict[str, Any], data: IntervalData) -> tuple[dict[str, Any], dict[str, Any]]:
        if str(request.get("engine")) != "kuru_lp":
            raise BacktestInputError("engine must be kuru_lp")
        market = str(request.get("market", "")).lower()
        if market != MARKET_ADDRESS:
            raise BacktestInputError("market must be the verified MON-USDC market")
        shape = str(request.get("shape", ""))
        if shape not in {"spot", "curve", "bid_ask"}:
            raise BacktestInputError("shape must be spot, curve, or bid_ask")
        params = data.parameters
        if data.start_market is not None:
            start_market = data.start_market
        else:
            cache = json.loads(checkpoint_cache_path(data.name).read_text(encoding="utf-8"))
            start_market = self._start_market_from_source(
                data.name, cache, validation_summary_path(data.name)
            )
        reference_price_raw = start_market["reference_price_raw"]
        submitted_reference = request.get("reference_price_raw")
        if submitted_reference is not None and int(submitted_reference) != reference_price_raw:
            raise BacktestInputError(
                "the submitted reference price does not match the selected interval",
                code="reference_price_mismatch",
                details={"expected_reference_price_raw": str(reference_price_raw)},
            )
        range_mode = str(request.get(
            "range_mode",
            "percentage" if request.get("lower_range_percent") is not None else "absolute",
        ))
        if range_mode not in {"percentage", "absolute"}:
            raise BacktestInputError("range mode must be percentage or absolute")
        lower_percent: str | None = None
        upper_percent: str | None = None
        if range_mode == "percentage":
            lower_percent = decimal_text(
                request.get("lower_range_percent"), 6, "lower range percentage", positive=False
            )
            upper_percent = decimal_text(
                request.get("upper_range_percent"), 6, "upper range percentage", positive=False
            )
            minimum_price_raw, maximum_price_raw = percentage_price_bounds(
                reference_price_raw, lower_percent, upper_percent, params["tick_size"]
            )
            submitted_minimum = request.get("minimum_price")
            submitted_maximum = request.get("maximum_price")
            if submitted_minimum is not None and raw_amount(
                decimal_text(submitted_minimum, PRICE_DECIMALS, "minimum price"), PRICE_DECIMALS
            ) != minimum_price_raw:
                raise BacktestInputError(
                    "the submitted minimum price does not match the percentage range",
                    code="range_bound_mismatch",
                    details={"expected_minimum_price_raw": str(minimum_price_raw)},
                )
            if submitted_maximum is not None and raw_amount(
                decimal_text(submitted_maximum, PRICE_DECIMALS, "maximum price"), PRICE_DECIMALS
            ) != maximum_price_raw:
                raise BacktestInputError(
                    "the submitted maximum price does not match the percentage range",
                    code="range_bound_mismatch",
                    details={"expected_maximum_price_raw": str(maximum_price_raw)},
                )
        else:
            entered_minimum = raw_amount(
                decimal_text(request.get("minimum_price"), PRICE_DECIMALS, "minimum price"),
                PRICE_DECIMALS,
            )
            entered_maximum = raw_amount(
                decimal_text(request.get("maximum_price"), PRICE_DECIMALS, "maximum price"),
                PRICE_DECIMALS,
            )
            minimum_price_raw = floor_to_tick(entered_minimum, params["tick_size"])
            maximum_price_raw = ceil_to_tick(entered_maximum, params["tick_size"])
        if minimum_price_raw >= maximum_price_raw:
            raise BacktestInputError("minimum price must be less than maximum price")
        minimum_price = raw_price_text(minimum_price_raw, params["price_precision"])
        maximum_price = raw_price_text(maximum_price_raw, params["price_precision"])
        fee_percent = decimal_text(request.get("fee_percent"), 4, "fee percentage")
        if Decimal(fee_percent) >= Decimal("100"):
            raise BacktestInputError("fee percentage must be less than 100")
        mon_amount = decimal_text(request.get("mon_amount"), BASE_DECIMALS, "MON amount")
        usdc_amount = decimal_text(request.get("usdc_amount"), QUOTE_DECIMALS, "USDC amount")
        driver = str(request.get("amount_driver", "both"))
        if driver not in {"both", "base", "quote"}:
            raise BacktestInputError("amount driver must be both, base, or quote")
        inclusion_delay = int(request.get("inclusion_delay_blocks", 1))
        if inclusion_delay < 1:
            raise BacktestInputError("inclusion delay must be at least one block")
        participation_value = request.get("max_transaction_participation_percent", 10)
        participation = None if participation_value is None else decimal_text(
            participation_value, 4, "participation percentage"
        )
        if participation is not None and Decimal(participation) > Decimal("100"):
            raise BacktestInputError("participation percentage must not exceed 100")
        impact = decimal_text(request.get("material_impact_threshold_percent", 25), 4, "impact threshold")
        fixed_action_cost = decimal_text(request.get("fixed_action_fee", 0), 6, "fixed action fee", positive=False)
        maker_fee_bps = decimal_text(request.get("maker_fee_bps", 0), 4, "maker fee", positive=False)
        best_ask = start_market["best_ask_price_raw"]
        normalized = {
            "engine": "kuru_lp",
            "engine_version": ENGINE_VERSION,
            "market": MARKET_ADDRESS,
            "market_symbol": "MON-USDC",
            "interval": data.name,
            "shape": shape,
            "range_mode": range_mode,
            "lower_range_percent": lower_percent,
            "upper_range_percent": upper_percent,
            "reference_price_raw": str(reference_price_raw),
            "reference_price": raw_price_text(reference_price_raw, params["price_precision"]),
            "minimum_price_raw": str(minimum_price_raw),
            "maximum_price_raw": str(maximum_price_raw),
            "minimum_price": minimum_price,
            "maximum_price": maximum_price,
            "fee_percent": fee_percent,
            "fee_pps": raw_amount(fee_percent, 4),
            "mon_amount": mon_amount,
            "usdc_amount": usdc_amount,
            "amount_driver": driver,
            "inclusion_delay_blocks": inclusion_delay,
            "max_transaction_participation_percent": participation,
            "material_impact_threshold_percent": impact,
            "fixed_action_fee": fixed_action_cost,
            "maker_fee_bps": maker_fee_bps,
        }
        if data.generalized_range:
            normalized["from_block"] = int(data.validation["interval"]["from_block"])
            normalized["to_block"] = int(data.validation["interval"]["to_block"])
        sdk_input = {
            "shape": shape,
            "minimumPrice": minimum_price,
            "maximumPrice": maximum_price,
            "feePercent": fee_percent,
            "monAmount": mon_amount,
            "usdcAmount": usdc_amount,
            "driver": driver,
            "bestAskPriceRaw": str(best_ask),
            "pricePrecision": str(params["price_precision"]),
            "sizePrecision": str(params["size_precision"]),
            "quoteAssetDecimals": params["quote_decimals"],
            "baseAssetDecimals": params["base_decimals"],
            "tickSize": str(params["tick_size"]),
            "minSize": str(params["min_size"]),
        }
        return normalized, sdk_input

    def run(
        self,
        request: dict[str, Any],
        *,
        progress_callback: Any = None,
        should_cancel: Any = None,
        event_limit: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            service_stages: dict[str, float] = {}
            interval_name = str(request.get("interval", ""))
            if progress_callback is not None:
                progress_callback({"stage": "loading_historical_events", "processed_events": 0, "total_events": 0})
            stage_start = time.perf_counter()
            if request.get("from_block") is not None or request.get("to_block") is not None:
                try:
                    from_block = int(request["from_block"])
                    to_block = int(request["to_block"])
                except (KeyError, TypeError, ValueError) as error:
                    raise BacktestInputError(
                        "from_block and to_block must both be integer block numbers"
                    ) from error
                data = self.load_range(
                    str(request.get("market", "")),
                    from_block,
                    to_block,
                    service_stages,
                )
                interval_name = data.name
            else:
                data = self.load_interval(interval_name, service_stages)
            service_stages["load_historical_events_seconds"] = time.perf_counter() - stage_start
            normalized, sdk_input = self.normalize_request(request, data)
            if progress_callback is not None:
                progress_callback({"stage": "generating_sdk_grid", "processed_events": 0, "total_events": len(data.events)})
            stage_start = time.perf_counter()
            grid = normalize_grid(canonical_sdk_grid(sdk_input))
            service_stages["sdk_grid_seconds"] = time.perf_counter() - stage_start
            submitted_grid = request.get("order_grid")
            if submitted_grid is None:
                raise BacktestInputError("the submitted SDK order grid is missing")
            if normalize_grid(submitted_grid) != grid:
                raise BacktestInputError("the submitted order grid does not match the canonical Kuru SDK grid")
            grid_validation = validate_sdk_grid(grid, data.parameters["min_size"])
            if not grid_validation["deployable"]:
                raise BacktestInputError(
                    "the generated grid contains an order below the market minimum size",
                    code="grid_below_market_minimum",
                    details={"grid_validation": grid_validation},
                )
            if len(grid["orders"]) > 20_000:
                raise BacktestInputError("the generated grid exceeds the 20,000-order safety limit")
            positions = [
                kuru_lp.LPPosition(
                    side=item["side"], price=int(item["price_raw"]),
                    liquidity=int(item["size_raw"]), flip_price=int(item["flip_price_raw"]),
                )
                for item in grid["orders"]
            ]
            distribution = kuru_lp.LPDistribution(
                bids=tuple(item for item in positions if item.side == "buy"),
                asks=tuple(item for item in positions if item.side == "sell"),
                quote_liquidity=int(grid["required_usdc_raw"]),
                base_liquidity=int(grid["required_mon_raw"]),
                min_size_error=False,
            )
            participation = normalized["max_transaction_participation_percent"]
            config = kuru_lp.LPConfig(
                shape=normalized["shape"],
                minimum_price_raw=int(grid["minimum_price_raw"]),
                maximum_price_raw=int(grid["maximum_price_raw"]),
                fee_pps=int(grid["fee_pps"]),
                quote_deposit_raw=raw_amount(normalized["usdc_amount"], QUOTE_DECIMALS),
                base_deposit_raw=raw_amount(normalized["mon_amount"], BASE_DECIMALS),
                maker_fee_bps=Decimal(normalized["maker_fee_bps"]),
                fixed_action_fee=Decimal(normalized["fixed_action_fee"]),
                inclusion_delay_blocks=int(normalized["inclusion_delay_blocks"]),
                max_transaction_participation_percent=(None if participation is None else Decimal(participation)),
                material_impact_threshold_percent=Decimal(normalized["material_impact_threshold_percent"]),
                min_size_raw=data.parameters["min_size"],
            )
            interval = data.validation["interval"]
            verified = {
                "event_source": data.validation["event_source"],
                "parity_status": data.validation["event_parity"]["status"],
                "event_count": data.validation["event_parity"]["rpc_event_count"],
                "validation_summary_sha256": data.source_sha256,
                "event_file_sha256": data.event_file_sha256,
                "checkpoint_sha256": data.checkpoint_sha256,
            }
            if event_limit is None:
                run_events = data.events
            elif data.generalized_range:
                from itertools import islice
                run_events = list(islice(data.events, event_limit))
            else:
                run_events = data.events[:event_limit]
            if not run_events:
                raise BacktestInputError("the selected event prefix is empty")
            run_end_block = int(interval["to_block"]) if event_limit is None else run_events[-1].block_number
            identity = {
                "engine_version": ENGINE_VERSION,
                "replay_schema_version": REPLAY_SCHEMA_VERSION,
                "normalized_configuration": normalized,
                "event_file_sha256": data.event_file_sha256,
                "checkpoint_sha256": data.checkpoint_sha256,
                "market_parameters": data.parameters,
                "order_grid": grid,
                "event_limit": event_limit,
            }
            identity_sha = sha256_value(identity)
            run_id = f"lp-{identity_sha[:20]}"
            run_dir = RUN_ROOT / run_id
            complete_path = run_dir / "complete.json"
            summary_path = run_dir / "summary.json"
            timeline_path = run_dir / "timeline.json"
            fills_path = run_dir / "fills.json"
            if (
                complete_path.exists()
                and summary_path.exists()
                and timeline_path.exists()
                and fills_path.exists()
            ):
                complete = json.loads(complete_path.read_text(encoding="utf-8"))
                saved = json.loads(summary_path.read_text(encoding="utf-8"))
                saved_timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
                saved_fills = json.loads(fills_path.read_text(encoding="utf-8"))
                if (
                    complete.get("identity_sha256") == identity_sha
                    and hashlib.sha256(canonical_bytes(saved)).hexdigest() == complete.get("summary_file_sha256")
                    and hashlib.sha256(canonical_bytes(saved_timeline)).hexdigest() == complete.get("timeline_file_sha256")
                    and hashlib.sha256(canonical_bytes(saved_fills)).hexdigest() == complete.get("fills_file_sha256")
                ):
                    return {**saved, "compact_timeline": saved_timeline}
            selected_stride = (
                max(1, (len(run_events) + MAX_COMPACT_FRAMES - 3) // (MAX_COMPACT_FRAMES - 2))
                if data.generalized_range and data.golden_fixture is None else 80
            )
            if (
                data.generalized_range
                and event_limit is None
                and data.reconstructed_start is not None
            ):
                summary, retained_timeline = self._lp_range_engine.run(
                    data.reconstructed_start,
                    run_events,
                    config,
                    distribution,
                    verified,
                    timeline_stride=selected_stride,
                    audit_invariants=False,
                    progress_callback=progress_callback,
                    should_cancel=should_cancel,
                )
            else:
                summary, retained_timeline = kuru_lp.run_lp(
                    data.book, run_events, int(interval["from_block"]), run_end_block,
                    config, distribution, data.parameters["price_precision"], data.parameters["size_precision"],
                    data.timestamps,
                    verified,
                    timeline_stride=selected_stride,
                    audit_invariants=False,
                    progress_callback=progress_callback,
                    should_cancel=should_cancel,
                )
            summary["normalized_configuration"] = normalized
            summary["initial_position"]["canonical_sdk_grid"] = grid
            summary["initial_position"]["grid_validation"] = grid_validation
            summary["performance"]["service_stage_seconds"] = service_stages
            if progress_callback is not None:
                progress_callback({"stage": "compacting_replay", "processed_events": len(run_events), "total_events": len(run_events)})
            stage_start = time.perf_counter()
            compact = compact_replays.build_compact_replay_from_records(
                retained_timeline,
                summary,
                interval_name=data.name,
                scenario_name=f"{normalized['shape']}_custom",
                maximum_frames=MAX_COMPACT_FRAMES,
            )
            summary["performance"]["stage_seconds"]["timeline_serialization"] = 0.0
            summary["performance"]["counters"]["timeline_serialized_bytes"] = 0
            service_stages["compact_replay_seconds"] = time.perf_counter() - stage_start
            flip_orders_created = sum(
                1 for record in retained_timeline for item in record.get("placements", [])
                if item.get("kind") == "flip_create"
            )
            semantic_timeline_hash = semantic_timeline_sha256(retained_timeline)
            fill_records_hash = sha256_value(summary["fills"])
            core_result = {
                "engine": "kuru_lp",
                "engine_version": ENGINE_VERSION,
                "normalized_configuration": normalized,
                "interval": summary["interval"],
                "generated_initial_bid_count": summary["initial_position"]["bid_count"],
                "generated_initial_ask_count": summary["initial_position"]["ask_count"],
                "starting_mon": summary["metrics"]["initial_inventory"],
                "final_mon": summary["metrics"]["final_inventory"],
                "starting_usdc": summary["metrics"]["initial_cash"],
                "final_usdc": summary["metrics"]["final_cash"],
                "fills": summary["metrics"]["fill_count"],
                "detailed_fills": {
                    "available": True,
                    "count": len(summary["fills"]),
                    "sha256": fill_records_hash,
                    "endpoint_suffix": "fills",
                },
                "flip_orders_created": flip_orders_created,
                "turnover": summary["metrics"]["turnover"],
                "gross_pnl": float(Decimal(str(summary["metrics"]["position_pnl"])) + Decimal(str(summary["metrics"]["maker_fees"])) + Decimal(str(summary["metrics"]["action_fees"]))),
                "net_pnl": summary["metrics"]["position_pnl"],
                "hold_pnl": summary["metrics"]["hold_pnl"],
                "excess_pnl": summary["metrics"]["excess_pnl"],
                "maximum_drawdown": summary["metrics"]["maximum_drawdown"],
                "maker_fees": summary["metrics"]["maker_fees"],
                "action_fees": summary["metrics"]["action_fees"],
                "earned_flip_spread": summary["metrics"]["earned_flip_spread"],
                "counterfactual": summary["counterfactual"],
                "performance": summary["performance"],
                "initial_order_grid": grid,
                "grid_validation": grid_validation,
                "results": compact["results"],
                "compact_timeline": compact["frames"],
                "semantic_timeline_schema_version": SEMANTIC_TIMELINE_SCHEMA_VERSION,
                "semantic_timeline_sha256": semantic_timeline_hash,
                "timeline_metadata": {
                    **compact["compaction"],
                    "full_engine_record_count": summary["timeline"]["full_record_count"],
                },
            }
            # Runtime measurements vary between identical runs. They are useful
            # diagnostics, but they must not change the deterministic result ID.
            summary_hash = sha256_value({
                key: value
                for key, value in core_result.items()
                if key not in {"compact_timeline", "performance"}
            })
            timeline_hash = sha256_value(core_result["compact_timeline"])
            response = {
                "run_id": run_id,
                "summary_sha256": summary_hash,
                "timeline_sha256": timeline_hash,
                **core_result,
            }
            if progress_callback is not None:
                progress_callback({"stage": "writing_cache", "processed_events": len(run_events), "total_events": len(run_events)})
            stage_start = time.perf_counter()
            run_dir.mkdir(parents=True, exist_ok=True)
            saved_summary = {key: value for key, value in response.items() if key != "compact_timeline"}
            atomic_write_bytes(summary_path, canonical_bytes(saved_summary) + b"\n")
            atomic_write_bytes(timeline_path, canonical_bytes(response["compact_timeline"]) + b"\n")
            atomic_write_bytes(fills_path, canonical_bytes(summary["fills"]) + b"\n")
            complete = {
                "schema_version": "2.0",
                "identity_sha256": identity_sha,
                "summary_file_sha256": hashlib.sha256(canonical_bytes(saved_summary)).hexdigest(),
                "timeline_file_sha256": hashlib.sha256(canonical_bytes(response["compact_timeline"])).hexdigest(),
                "fills_file_sha256": fill_records_hash,
            }
            atomic_write_bytes(complete_path, canonical_bytes(complete) + b"\n")
            return response


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-interval", choices=("relatively_quiet", "relatively_active", "directional"))
    parser.add_argument("--rpc-url", default=l3.DEFAULT_RPC_URL)
    args = parser.parse_args()
    if args.prepare_interval:
        print(prepare_interval_cache(args.prepare_interval, args.rpc_url))
        return 0
    parser.error("select --prepare-interval")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
