"""Closed browser requests do not trigger another response or hide server bugs."""
import io
import pytest
from kuru_backtester.server.http_response import QuietDisconnectMixin
from kuru_backtester.server.backtest_server import BacktestHandler
from kuru_backtester.server.full_history_monitor import MonitorHandler


@pytest.mark.parametrize("handler_type", [BacktestHandler, MonitorHandler])
@pytest.mark.parametrize("stage", ["headers", "body", "flush"])
@pytest.mark.parametrize("error_type", [BrokenPipeError, ConnectionResetError, ConnectionAbortedError])
def test_disconnect_is_safe_at_each_response_stage(handler_type, stage, error_type):
    handler = object.__new__(handler_type)
    responses = []
    handler.send_response = responses.append
    handler.send_header = lambda *args: None
    def broken():
        raise error_type("browser closed")
    handler.end_headers = broken if stage == "headers" else lambda: None
    class Writer:
        def write(self, value):
            if stage == "body":
                broken()
        def flush(self):
            if stage == "flush":
                broken()
    handler.wfile = Writer()
    assert handler.send_json(200, {"phase": "idle"}) is None
    assert handler.close_connection is True
    assert responses == [200]


def test_successful_response_returns_existing_transfer_metrics():
    handler = QuietDisconnectMixin()
    handler.send_response = lambda status: None
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler.wfile = io.BytesIO()
    result = handler.send_json(200, {"phase": "idle"})
    assert handler.wfile.getvalue() == b'{"phase":"idle"}'
    assert result["encoded_bytes"] == len(handler.wfile.getvalue())
    assert result["socket_write_seconds"] >= 0
    with pytest.raises(TypeError):
        handler.send_json(200, {"invalid": object()})


def test_static_request_disconnect_is_safe_but_other_failures_propagate():
    class Base:
        def handle_one_request(self):
            raise self.failure
    class Handler(QuietDisconnectMixin, Base):
        pass
    handler = Handler()
    handler.failure = BrokenPipeError()
    handler.handle_one_request()
    assert handler.close_connection
    handler.failure = ValueError("server bug")
    with pytest.raises(ValueError, match="server bug"):
        handler.handle_one_request()
