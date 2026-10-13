"""Transport retries do not change events, queues, or accounting."""
import io
import json
from email.message import Message
from urllib.error import HTTPError

import pytest

from kuru_backtester.engine import rebuild_from_chain as chain


@pytest.mark.parametrize("retry_after,expected", [
    ("4", 4),
    ("Mon, 12 Oct 2026 12:00:04 GMT", 4),
    (None, 15),
    ("invalid", 15),
    ("inf", 15),
    ("NaN", 15),
])
def test_rpc_429_respects_retry_after(monkeypatch, retry_after, expected):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    responses = [HTTPError("https://example.invalid", 429, "quota", headers, None),
                 io.BytesIO(json.dumps({"result": "ok"}).encode())]
    def open_request(*args, **kwargs):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
    waits = []
    monkeypatch.setattr(chain, "urlopen", open_request)
    monkeypatch.setattr(chain.time, "sleep", waits.append)
    monkeypatch.setattr(chain.time, "time", lambda: 1791806400)
    assert chain.RpcClient("https://example.invalid", retries=2).call("test", []) == "ok"
    assert waits == [expected]


def test_rpc_rate_limit_backoff_is_bounded_and_exhaustion_fails(monkeypatch):
    def always_limited(*args, **kwargs):
        raise HTTPError("https://example.invalid", 429, "quota", Message(), None)
    waits = []
    monkeypatch.setattr(chain, "urlopen", always_limited)
    monkeypatch.setattr(chain.time, "sleep", waits.append)
    with pytest.raises(chain.RpcError, match="5 attempts"):
        chain.RpcClient("https://example.invalid", retries=5).call("test", [])
    assert waits == [15, 30, 60, 120]


def test_non_rate_limit_keeps_original_retry_timing(monkeypatch):
    def failed(*args, **kwargs):
        raise OSError("offline")
    waits = []
    monkeypatch.setattr(chain, "urlopen", failed)
    monkeypatch.setattr(chain.time, "sleep", waits.append)
    with pytest.raises(chain.RpcError):
        chain.RpcClient("https://example.invalid", retries=3).call("test", [])
    assert waits == [1, 2]


def test_rpc_trace_reports_methods_and_retry_without_credentials(monkeypatch):
    responses = [HTTPError("https://secret.invalid/token", 429, "quota", Message(), None),
                 io.BytesIO(b'{"result":"ok"}')]
    def open_request(*args, **kwargs):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(chain, "urlopen", open_request)
    monkeypatch.setattr(chain.time, "sleep", lambda _: None)
    rpc = chain.RpcClient("https://secret.invalid/token", retries=2)
    actions = []
    rpc.observer = actions.append
    assert rpc.call("eth_getBlockByNumber", ["0xa", False]) == "ok"
    assert actions[0]["kind"] == "rpc_retry"
    assert actions[0]["http_status"] == 429
    assert actions[1]["methods"] == {"eth_getBlockByNumber": 1}
    assert "secret" not in json.dumps(actions)


def test_timestamp_batches_match_ids_deduplicate_and_reduce_requests(monkeypatch):
    rpc = chain.RpcClient("https://example.invalid")
    requests = []
    def post(batch):
        requests.append(batch)
        return [{"id": item["id"], "result": {"number": item["params"][0],
                 "timestamp": hex(3600 + int(item["params"][0], 16))}} for item in reversed(batch)]
    monkeypatch.setattr(rpc, "_post", post)
    result = rpc.block_timestamps([*range(1073), 0, 5])
    assert result == {block: 3600 + block for block in range(1073)}
    assert len(requests) == 22  # Previously 108 HTTP requests for these blocks.
    assert sum(map(len, requests)) == 1073


@pytest.mark.parametrize("http_limit", [False, True])
def test_timestamp_batch_limit_shrinks_and_remembers(monkeypatch, http_limit):
    rpc = chain.RpcClient("https://example.invalid")
    sizes = []
    def post(batch):
        sizes.append(len(batch))
        if len(batch) > 10:
            if http_limit:
                raise chain.RpcBatchLimitError("payload limit")
            return {"error": {"message": "Batch size exceeds maximum of 10"}}
        return [{"id": item["id"], "result": {"timestamp": "0x1"}} for item in batch]
    monkeypatch.setattr(rpc, "_post", post)
    assert len(rpc.block_timestamps(range(101))) == 101
    assert sizes[:4] == [50, 25, 12, 6]
    sizes.clear()
    assert len(rpc.block_timestamps(range(20))) == 20
    assert max(sizes) == 6


def test_http_payload_limit_does_not_retry_same_batch(monkeypatch):
    calls = []
    def too_large(*args, **kwargs):
        calls.append(1)
        raise HTTPError("https://example.invalid", 413, "too large", Message(), None)
    monkeypatch.setattr(chain, "urlopen", too_large)
    with pytest.raises(chain.RpcBatchLimitError):
        chain.RpcClient("https://example.invalid").batch([("test", [])] * 50)
    assert len(calls) == 1


@pytest.mark.parametrize("result", [None, {"number": "0x2", "timestamp": "0x3"}])
def test_timestamp_missing_or_wrong_header_fails(monkeypatch, result):
    rpc = chain.RpcClient("https://example.invalid")
    monkeypatch.setattr(rpc, "batch", lambda calls: [result])
    with pytest.raises(chain.RpcError):
        rpc.block_timestamps([1])


def test_rpc_pacing_uses_monotonic_time(monkeypatch):
    now, waits = [0.0], []
    def sleep(delay):
        waits.append(delay)
        now[0] += delay
    monkeypatch.setattr(chain.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(chain.time, "sleep", sleep)
    monkeypatch.setattr(chain, "urlopen", lambda *a, **kw: io.BytesIO(b'{"result":"ok"}'))
    rpc = chain.RpcClient("https://example.invalid", min_request_interval=0.1)
    for _ in range(3):
        rpc.call("test", [])
    assert waits == pytest.approx([0.1, 0.1])


def test_unrelated_batch_error_does_not_shrink_or_repeat(monkeypatch):
    rpc = chain.RpcClient("https://example.invalid")
    calls = []
    def post(batch):
        calls.append(batch)
        return [{"id": item["id"], "error": {"message": "archive unavailable"}} for item in batch]
    monkeypatch.setattr(rpc, "_post", post)
    with pytest.raises(chain.RpcError, match="archive unavailable"):
        rpc.block_timestamps(range(20))
    assert len(calls) == 1
    assert rpc.timestamp_batch_size == 50
