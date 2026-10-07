#!/usr/bin/env python3
"""Serve the frontend and the local Kuru LP backtest API."""

from __future__ import annotations

import argparse
import json
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from backtest_service import BacktestInputError, BacktestService
from backtest_jobs import BacktestJobManager, JobError


ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"


class BacktestHandler(SimpleHTTPRequestHandler):
    service: BacktestService
    jobs: BacktestJobManager

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(FRONTEND), **kwargs)

    def send_json(self, status: int, payload: Any) -> dict[str, Any]:
        encode_start = time.perf_counter()
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        encode_seconds = time.perf_counter() - encode_start
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        write_start = time.perf_counter()
        self.wfile.write(body)
        self.wfile.flush()
        return {
            "encoded_bytes": len(body),
            "json_encoding_seconds": encode_seconds,
            "socket_write_seconds": time.perf_counter() - write_start,
        }

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/intervals":
            try:
                self.send_json(HTTPStatus.OK, self.service.interval_metadata())
            except Exception as error:  # The API must return a visible error to the UI.
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
            return
        parts = path.strip("/").split("/")
        if len(parts) in {3, 4} and parts[:2] == ["api", "backtests"]:
            run_id = parts[2]
            owner = self.headers.get("X-Backtest-Owner")
            try:
                if len(parts) == 3:
                    self.send_json(HTTPStatus.OK, self.jobs.status(run_id, owner))
                elif parts[3] == "result":
                    result = self.jobs.result(run_id, owner)
                    metrics = self.send_json(HTTPStatus.OK, result)
                    self.jobs.record_response_transmission(run_id, owner, metrics)
                elif parts[3] == "fills":
                    self.send_json(HTTPStatus.OK, self.jobs.fills(run_id, owner))
                else:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "unknown API path"})
            except JobError as error:
                self.send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        super().do_GET()

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/backtests":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "unknown API path"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise BacktestInputError("request body size is invalid")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise BacktestInputError("request body must be a JSON object")
            created = self.jobs.create(payload)
            self.send_json(HTTPStatus.ACCEPTED, created)
        except BacktestInputError as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": error.payload()})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception as error:  # Keep backend failures visible. Never return demo data.
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "backtests"]:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "unknown API path"})
            return
        try:
            self.send_json(
                HTTPStatus.OK,
                self.jobs.cancel(parts[2], self.headers.get("X-Backtest-Owner")),
            )
        except JobError as error:
            self.send_json(HTTPStatus.CONFLICT, {"error": str(error)})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    service = BacktestService()
    jobs = BacktestJobManager()
    BacktestHandler.service = service
    BacktestHandler.jobs = jobs
    server = ThreadingHTTPServer((args.host, args.port), BacktestHandler)
    print(f"Kuru backtest server: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        jobs.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
