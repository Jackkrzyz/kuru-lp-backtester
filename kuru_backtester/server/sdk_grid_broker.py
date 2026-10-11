#!/usr/bin/env python3
"""Share one warm Kuru SDK Node.js process across isolated backtest jobs."""

from __future__ import annotations

import json
import queue
import secrets
import subprocess
import threading
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class SdkWorkerError(RuntimeError):
    """Report a failed or invalid response from the local SDK worker."""


class PersistentSdkProcess:
    """Own one line-oriented Node.js worker and restart it after a failure."""

    def __init__(self, node: str, script: Path, cwd: Path, timeout: float = 30.0) -> None:
        self.node = node
        self.script = script
        self.cwd = cwd
        self.timeout = timeout
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[str | None] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=20)
        self._lock = threading.Lock()
        self._request_number = 0

    def _start(self) -> None:
        self._stop()
        process = subprocess.Popen(
            [self.node, str(self.script)],
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._process = process
        responses: queue.Queue[str | None] = queue.Queue()
        self._responses = responses

        def read_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                responses.put(line)
            responses.put(None)

        def read_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                self._stderr.append(line.rstrip())

        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()

    def _stop(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)

    def close(self) -> None:
        with self._lock:
            self._stop()

    def _request_once(self, sdk_input: dict[str, Any]) -> dict[str, Any]:
        if self._process is None or self._process.poll() is not None:
            self._start()
        assert self._process is not None and self._process.stdin is not None
        self._request_number += 1
        request_id = f"sdk-{self._request_number}"
        self._process.stdin.write(json.dumps(
            {"id": request_id, "input": sdk_input},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n")
        self._process.stdin.flush()
        try:
            line = self._responses.get(timeout=self.timeout)
        except queue.Empty as error:
            raise SdkWorkerError("the persistent Kuru SDK worker timed out") from error
        if line is None:
            detail = "; ".join(self._stderr) or "no stderr output"
            raise SdkWorkerError(f"the persistent Kuru SDK worker stopped: {detail}")
        response = json.loads(line)
        if response.get("id") != request_id:
            raise SdkWorkerError("the persistent Kuru SDK worker returned the wrong request ID")
        if response.get("error"):
            raise SdkWorkerError(str(response["error"]))
        grid = response.get("grid")
        if not isinstance(grid, dict):
            raise SdkWorkerError("the persistent Kuru SDK worker returned no grid")
        return grid

    def request(self, sdk_input: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            try:
                return self._request_once(sdk_input)
            except (BrokenPipeError, OSError, ValueError, json.JSONDecodeError, SdkWorkerError):
                self._stop()
                return self._request_once(sdk_input)


class SdkGridBroker:
    """Expose the warm SDK worker to child jobs on authenticated localhost HTTP."""

    def __init__(self, node: str, script: Path, cwd: Path) -> None:
        self.token = secrets.token_urlsafe(32)
        self.worker = PersistentSdkProcess(node, script, cwd)
        worker = self.worker
        token = self.token

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                if self.path != "/grid" or self.headers.get("Authorization") != f"Bearer {token}":
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 100_000:
                        raise ValueError("SDK request body size is invalid")
                    sdk_input = json.loads(self.rfile.read(length))
                    if not isinstance(sdk_input, dict):
                        raise ValueError("SDK request must be a JSON object")
                    body = json.dumps(
                        {"grid": worker.request(sdk_input)},
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode()
                    self.send_response(HTTPStatus.OK)
                except Exception as error:
                    body = json.dumps({"error": str(error)}).encode()
                    self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}/grid"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.worker.close()

