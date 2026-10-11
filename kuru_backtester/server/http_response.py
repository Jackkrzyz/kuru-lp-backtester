"""Local HTTP responses tolerate a browser closing a request."""
import json
import time


class QuietDisconnectMixin:
    def handle_one_request(self):
        try:
            return super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # This also covers static files and the base handler's final flush.
            self.close_connection = True

    def send_json(self, status, payload):
        started = time.perf_counter()
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        encode_seconds = time.perf_counter() - started
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            write_start = time.perf_counter()
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Do not send a second error response or record a successful transfer.
            self.close_connection = True
            return None
        return {"encoded_bytes": len(body), "json_encoding_seconds": encode_seconds,
                "socket_write_seconds": time.perf_counter() - write_start}
