#!/usr/bin/env python3
"""Serve read-only indexing telemetry on localhost. Never start downloads."""
import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from kuru_backtester.paths import ROOT
from kuru_backtester.server.data_suite import DataSuite
from kuru_backtester.server.http_response import QuietDisconnectMixin
PROGRESS_PATH = ROOT / ".cache/full_history/progress.json"


def read_progress(path: Path) -> dict:
    if not path.exists():
        return {"phase": "idle", "notice": "No indexing job has started. Start full_history.py in another terminal."}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return {"phase": "error", "error": f"Cannot read indexing status: {error}"}


class MonitorHandler(QuietDisconnectMixin, SimpleHTTPRequestHandler):
    progress_path = PROGRESS_PATH
    data_suite = DataSuite()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "frontend"), **kwargs)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/indexing-progress":
            self.send_json(200, read_progress(self.progress_path))
            return
        if path == "/api/data-suite":
            payload = {**self.data_suite.inventory_snapshot(), "controls_available": False, "jobs": []}
            self.send_json(200, payload)
            return
        if path == "/":
            self.path = "/indexing.html"
        elif path not in {"/indexing.html", "/indexing.css", "/indexing.js", "/data_suite.js", "/styles.css"}:
            self.send_error(404)
            return
        super().do_GET()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--job-root", type=Path, default=PROGRESS_PATH.parent)
    args = parser.parse_args()
    MonitorHandler.progress_path = args.job_root / "progress.json"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), MonitorHandler)
    print(f"Indexing monitor: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        MonitorHandler.data_suite.close()


if __name__ == "__main__":
    main()
