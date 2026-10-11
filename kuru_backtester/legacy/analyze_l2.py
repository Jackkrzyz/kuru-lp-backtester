"""Download one day of Kuru L2 snapshots and calculate top-of-book metrics."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd


MARKET_ADDRESS = "0x065c9d28e428a0db40191a54d33d5b7c71a9c394"
DEFAULT_DATE = "2026-05-01"
BASE_URL = "https://kuru-l2-snapshots.s3.amazonaws.com/l2-snapshots"
PRICE_SCALE = Decimal("1e18")
EXPECTED_COLUMNS = {"timestamp", "bids", "asks"}


def validate_date(value: str) -> str:
    """Return an ISO date, or report an argparse error."""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def validate_market(value: str) -> str:
    """Return a normalized Ethereum address, or report an argparse error."""
    normalized = value.lower()
    if not re.fullmatch(r"0x[0-9a-f]{40}", normalized):
        raise argparse.ArgumentTypeError("market must be a 20-byte hex address")
    return normalized


def snapshot_url(market: str, day: str) -> str:
    """Build the public Kuru snapshot URL."""
    filename = f"{market}_{day}.parquet"
    return f"{BASE_URL}/{market}/{filename}"


def download_snapshot(
    market: str, day: str, output_dir: Path, force: bool = False
) -> Path:
    """Download a snapshot atomically and return its local path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{market}_{day}.parquet"
    if destination.exists() and not force:
        print(f"Use cached snapshot: {destination}")
        return destination

    url = snapshot_url(market, day)
    request = Request(url, headers={"User-Agent": "kuru-market-making-backtester/0.1"})
    temporary_path: Path | None = None

    try:
        with urlopen(request, timeout=60) as response:
            with NamedTemporaryFile(
                mode="wb", dir=output_dir, prefix="snapshot-", suffix=".part", delete=False
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                while chunk := response.read(1024 * 1024):
                    temporary_file.write(chunk)
        temporary_path.replace(destination)
    except HTTPError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if exc.code == 403:
            raise RuntimeError(
                f"No public snapshot is available for {market} on {day}."
            ) from exc
        raise RuntimeError(f"Kuru returned HTTP {exc.code} for {url}.") from exc
    except (OSError, URLError) as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise RuntimeError(f"Could not download {url}: {exc}") from exc

    print(f"Downloaded snapshot: {destination}")
    return destination


def _json_levels(raw: Any) -> list[Any] | None:
    """Parse one JSON book side. Return None when the value is malformed."""
    if raw is None:
        return []
    if isinstance(raw, float) and math.isnan(raw):
        return []
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(raw, list):
        return None
    return raw


def _level_price(level: Any) -> Decimal | None:
    """Get a human-readable price from an array or object level."""
    raw_price: Any
    if isinstance(level, (list, tuple)) and level:
        raw_price = level[0]
    elif isinstance(level, dict):
        raw_price = next(
            (level[key] for key in ("price", "p") if key in level), None
        )
    else:
        return None

    price_text = str(raw_price)
    try:
        price = Decimal(price_text)
    except (InvalidOperation, TypeError, ValueError):
        return None
    # Kuru's archive has used both raw 1e18 integers and pre-scaled decimal
    # strings. Decimal strings contain a point; raw integer units do not.
    if "." not in price_text and "e" not in price_text.lower():
        price /= PRICE_SCALE
    if not price.is_finite() or price <= 0:
        return None
    return price


def best_price(raw: Any, side: str) -> float:
    """Return the best scaled price, or NaN for an empty or malformed side."""
    levels = _json_levels(raw)
    if levels is None:
        return math.nan

    prices = [price for level in levels if (price := _level_price(level)) is not None]
    if not prices:
        return math.nan
    best = max(prices) if side == "bid" else min(prices)
    return float(best)


def build_metrics_frame(raw_frame: pd.DataFrame) -> pd.DataFrame:
    """Create top-of-book metrics and preserve invalid rows as NaN values."""
    missing = EXPECTED_COLUMNS.difference(raw_frame.columns)
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"Snapshot does not contain required columns: {names}")

    frame = pd.DataFrame()
    frame["timestamp"] = pd.to_datetime(
        raw_frame["timestamp"], unit="s", utc=True, errors="coerce"
    )
    frame["best_bid"] = raw_frame["bids"].map(lambda value: best_price(value, "bid"))
    frame["best_ask"] = raw_frame["asks"].map(lambda value: best_price(value, "ask"))
    frame["midpoint"] = (frame["best_bid"] + frame["best_ask"]) / 2
    frame["spread"] = frame["best_ask"] - frame["best_bid"]
    frame["spread_percent"] = (frame["spread"] / frame["midpoint"]) * 100
    frame.loc[frame["midpoint"] <= 0, "spread_percent"] = math.nan
    return frame


def load_metrics(snapshot_path: Path) -> pd.DataFrame:
    """Read the snapshot and return the calculated metrics DataFrame."""
    try:
        raw_frame = pd.read_parquet(
            snapshot_path, columns=["timestamp", "bids", "asks"]
        )
    except Exception as exc:
        raise RuntimeError(f"Could not read Parquet file {snapshot_path}: {exc}") from exc

    if raw_frame.empty:
        raise RuntimeError(f"Snapshot contains no rows: {snapshot_path}")
    return build_metrics_frame(raw_frame)


def save_chart(frame: pd.DataFrame, chart_path: Path) -> None:
    """Save a two-panel midpoint and spread chart."""
    chart_path.parent.mkdir(parents=True, exist_ok=True)
    valid_time = frame["timestamp"].notna()
    plot_frame = frame.loc[valid_time]
    if plot_frame.empty:
        raise RuntimeError("No valid timestamps are available for the chart.")

    figure, (midpoint_axis, spread_axis) = plt.subplots(
        2, 1, sharex=True, figsize=(12, 7), constrained_layout=True
    )
    midpoint_axis.plot(
        plot_frame["timestamp"], plot_frame["midpoint"], color="#2457C5", linewidth=1
    )
    midpoint_axis.set_ylabel("Midpoint (USDC)")
    midpoint_axis.grid(alpha=0.25)

    spread_axis.plot(
        plot_frame["timestamp"], plot_frame["spread"], color="#D95F02", linewidth=1
    )
    spread_axis.set_ylabel("Spread (USDC)")
    spread_axis.set_xlabel("Time (UTC)")
    spread_axis.grid(alpha=0.25)

    figure.suptitle("Kuru MON-USDC midpoint and spread")
    figure.savefig(chart_path, dpi=150)
    plt.close(figure)
    print(f"Saved chart: {chart_path}")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Download one day of Kuru L2 data and calculate top-of-book metrics."
    )
    parser.add_argument("--date", type=validate_date, default=DEFAULT_DATE)
    parser.add_argument("--market", type=validate_market, default=MARKET_ADDRESS)
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument(
        "--force", action="store_true", help="Download the snapshot again if it exists."
    )
    return parser.parse_args()


def main() -> int:
    """Run the one-day analysis."""
    args = parse_args()
    try:
        snapshot_path = download_snapshot(
            args.market, args.date, args.output_dir, force=args.force
        )
        metrics = load_metrics(snapshot_path)
        print("\nFirst five rows:")
        print(metrics.head().to_string(index=False))

        invalid_books = (metrics["best_bid"].isna() | metrics["best_ask"].isna()).sum()
        invalid_times = metrics["timestamp"].isna().sum()
        if invalid_books or invalid_times:
            print(
                f"\nKept {invalid_books} rows with an empty or malformed book side "
                f"and {invalid_times} rows with an invalid timestamp as NaN/NaT.",
                file=sys.stderr,
            )

        chart_path = args.output_dir / f"mon_usdc_{args.date}_midpoint_spread.png"
        save_chart(metrics, chart_path)
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

