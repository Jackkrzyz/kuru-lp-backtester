"""Shared source-checkout paths. Generated data stays in its existing location."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "data" / "reports"
LEGACY_REPLAYS = ROOT / "tests" / "fixtures" / "legacy_replays"
