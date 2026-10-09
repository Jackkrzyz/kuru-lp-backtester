#!/usr/bin/env python3
"""Serve the frontend and the local Kuru LP backtest API."""

from __future__ import annotations

import argparse
import ipaddress
import json
import secrets
import socket
import subprocess
import time
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from backtest_service import BacktestInputError, BacktestService
from backtest_jobs import BacktestJobManager, JobError


ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"


def browser_urls(host: str, port: int, access_token: str | None) -> list[str]:
    """Print usable interface addresses instead of a wildcard bind address."""
    addresses = [host]
    if host == "0.0.0.0":
        candidates: list[str] = []
        try:
            result = subprocess.run(
                ["hostname", "-I"], capture_output=True, text=True, timeout=2, check=True
            )
            candidates.extend(result.stdout.split())
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            candidates.extend(socket.gethostbyname_ex(socket.gethostname())[2])
        except OSError:
            pass
        addresses = []
        for value in candidates:
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if address.version == 4 and not address.is_loopback and not address.is_unspecified:
                if value not in addresses:
                    addresses.append(value)
        addresses = addresses or ["127.0.0.1"]
    suffix = f"/?access_token={access_token}" if access_token else "/"
    return [f"http://{address}:{port}{suffix}" for address in addresses]


class BacktestHandler(SimpleHTTPRequestHandler):
    service: BacktestService
    jobs: BacktestJobManager
    access_token: str | None = None

    def authorized(self) -> bool:
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host', '')}":
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        if self.access_token is None:
            return True
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            supplied = cookie.get("kuru_access")
            return supplied is not None and secrets.compare_digest(supplied.value, self.access_token)
        except Exception:
            return False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(FRONTEND), **kwargs)

    def end_headers(self) -> None:
        """Do not let the local product UI retain stale code or results."""
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

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
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" and self.access_token is not None:
            token = parse_qs(parsed.query).get("access_token", [""])[0]
            if secrets.compare_digest(token, self.access_token):
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Set-Cookie", f"kuru_access={self.access_token}; HttpOnly; SameSite=Strict; Path=/")
                self.send_header("Location", "/")
                self.end_headers()
                return
        if not self.authorized():
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Open the access URL printed by the server."})
            return
        if path == "/api/intervals":
            try:
                self.send_json(HTTPStatus.OK, self.service.interval_metadata())
            except Exception as error:  # The API must return a visible error to the UI.
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
            return
        if path == "/api/range-metadata":
            try:
                query = parse_qs(parsed.query)
                market = query.get("market", [""])[0]
                from_block = int(query.get("from_block", [""])[0])
                to_block = int(query.get("to_block", [""])[0])
                self.send_json(
                    HTTPStatus.OK,
                    self.service.range_metadata(market, from_block, to_block),
                )
            except BacktestInputError as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": error.payload()})
            except (TypeError, ValueError):
                self.send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "from_block and to_block must be integer block numbers"},
                )
            except Exception as error:
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
        if not self.authorized():
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "server access token is required"})
            return
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
        except JobError as error:
            self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": str(error)})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception as error:  # Keep backend failures visible. Never return demo data.
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})

    def do_DELETE(self) -> None:
        if not self.authorized():
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "server access token is required"})
            return
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
    parser.add_argument("--maximum-workers", type=int, default=2)
    args = parser.parse_args()
    service = BacktestService()
    if args.maximum_workers < 1:
        parser.error("maximum-workers must be at least one")
    jobs = BacktestJobManager(maximum_workers=args.maximum_workers)
    BacktestHandler.service = service
    BacktestHandler.jobs = jobs
    BacktestHandler.access_token = None if args.host in {"127.0.0.1", "localhost", "::1"} else secrets.token_urlsafe(32)
    server = ThreadingHTTPServer((args.host, args.port), BacktestHandler)
    print("Kuru backtest server. Open in your browser:", flush=True)
    for url in browser_urls(args.host, server.server_port, BacktestHandler.access_token):
        print(url, flush=True)
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
