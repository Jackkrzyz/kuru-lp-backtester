"""Read completed local LP runs. Loading a saved run never starts simulation."""
import hashlib
import json
from pathlib import Path
import re

from kuru_backtester.server.backtest_service import RUN_ROOT, canonical_bytes


class SavedBacktests:
    def __init__(self, root=RUN_ROOT):
        self.root = Path(root)

    def _directory(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"lp-[0-9a-f]{20}", run_id):
            raise ValueError("Invalid saved run ID")
        directory = self.root / run_id
        if directory.is_symlink() or not directory.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Saved run path must stay inside the result cache")
        return directory

    def _read(self, directory, name):
        path = directory / name
        if path.is_symlink():
            raise ValueError("Saved result files must not be links")
        return json.loads(path.read_text(encoding="utf-8"))

    def _summary(self, directory):
        complete = self._read(directory, "complete.json")
        summary = self._read(directory, "summary.json")
        if (not isinstance(complete, dict) or not isinstance(summary, dict)
                or complete.get("schema_version") != "2.0" or summary.get("run_id") != directory.name
                or hashlib.sha256(canonical_bytes(summary)).hexdigest() != complete.get("summary_file_sha256")):
            raise ValueError("Saved summary hash does not match")
        if not isinstance(summary.get("normalized_configuration"), dict) or not isinstance(summary.get("results"), dict):
            raise ValueError("Saved summary is missing its configuration or results")
        if not (directory / "timeline.json").is_file():
            raise ValueError("Saved replay is missing")
        return summary, complete

    def list(self):
        rows, skipped = [], 0
        for directory in sorted(self.root.glob("lp-*"), key=lambda p: p.name):
            try:
                directory = self._directory(directory.name)
                summary, _ = self._summary(directory)
                rows.append({"run_id": directory.name, "configuration": summary["normalized_configuration"],
                             "interval": summary.get("interval"),
                             "results": summary["results"], "summary_sha256": summary["summary_sha256"],
                             "timeline_sha256": summary["timeline_sha256"],
                             "saved_at": (directory / "complete.json").stat().st_mtime})
            except (OSError, ValueError, KeyError):
                skipped += 1
        return {"runs": sorted(rows, key=lambda row: (-row["saved_at"], row["run_id"])), "skipped_invalid": skipped}

    def load(self, run_id):
        directory = self._directory(run_id)
        summary, complete = self._summary(directory)
        timeline = self._read(directory, "timeline.json")
        digest = hashlib.sha256(canonical_bytes(timeline)).hexdigest()
        if digest != complete.get("timeline_file_sha256") or digest != summary.get("timeline_sha256"):
            raise ValueError("Saved timeline hash does not match")
        if not isinstance(timeline, list) or not timeline:
            raise ValueError("Saved replay has no frames")
        return {**summary, "compact_timeline": timeline}
