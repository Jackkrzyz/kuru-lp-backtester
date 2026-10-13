#!/usr/bin/env python3
"""Serve read-only indexing telemetry on localhost. Never start downloads."""
import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

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
        if path in {"/kurulabnobg.png", "/kurulabbanner1.png"}:
            name = "Kurulabnobg.png" if path == "/kurulabnobg.png" else "kurulabbanner1.png"
            body = (ROOT / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/indexing-progress":
            self.send_json(200, read_progress(self.progress_path))
            return
        if path == "/api/data-suite":
            payload = {**self.data_suite.status(), "controls_available": False,
                       "observed_progress": read_progress(self.progress_path)}
            self.send_json(200, payload)
            return
        if path == "/api/market-chart":
            try:
                query = parse_qs(urlparse(self.path).query)
                first = int(query["from_time"][0]) if "from_time" in query else None
                last = int(query["to_time"][0]) if "to_time" in query else None
                self.send_json(200, self.data_suite.market_chart.snapshot(first, last, int(query.get("seconds", ["86400"])[0])))
            except (ValueError, TypeError) as error:
                self.send_json(400, {"error": str(error)})
            return
        if path == "/":
            self.path = "/indexing.html"
        elif path not in {"/indexing.html", "/indexing.css", "/data_suite.js", "/styles.css", "/page_loading.js",
                          "/terminal.css", "/terminal.js", "/terminal_jobs.js", "/terminal_chart.js", "/chart_library.js", "/chart-license.txt"}:
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
    print(f"kuruLab monitor: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        MonitorHandler.data_suite.close()


if __name__ == "__main__":
    main()
