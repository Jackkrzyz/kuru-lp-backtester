"""Saved results are read-only and must have intact completion hashes."""
import hashlib
import json

import pytest

from kuru_backtester.server.saved_backtests import SavedBacktests
from kuru_backtester.server.backtest_service import canonical_bytes


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


@pytest.fixture
def saved(tmp_path):
    run_id = "lp-" + "a" * 20
    directory = tmp_path / run_id
    directory.mkdir()
    timeline = [{"timestamp": "2026-01-01T00:00:00Z", "cumulative_pnl": "1"}]
    summary = {"run_id": run_id, "normalized_configuration": {"shape": "spot"},
               "results": {"strategy_pnl": "1"}, "summary_sha256": "deterministic-core-hash",
               "timeline_sha256": digest(timeline)}
    complete = {"schema_version": "2.0", "summary_file_sha256": digest(summary), "timeline_file_sha256": digest(timeline)}
    for name, value in (("summary", summary), ("timeline", timeline), ("complete", complete)):
        (directory / f"{name}.json").write_bytes(canonical_bytes(value) + b"\n")
    return SavedBacktests(tmp_path), run_id, directory, summary, timeline


def test_saved_list_is_small_and_loading_preserves_exact_result(saved):
    service, run_id, _, summary, timeline = saved
    listing = service.list()
    assert listing["runs"][0]["run_id"] == run_id
    assert "compact_timeline" not in listing["runs"][0]
    assert service.load(run_id) == {**summary, "compact_timeline": timeline}


def test_saved_load_rejects_changed_timeline(saved):
    service, run_id, directory, _, _ = saved
    (directory / "timeline.json").write_text("[]")
    with pytest.raises(ValueError, match="timeline hash"):
        service.load(run_id)


def test_incomplete_or_changed_summary_is_not_listed(saved):
    service, _, directory, _, _ = saved
    (directory / "summary.json").write_text("{}")
    assert service.list() == {"runs": [], "skipped_invalid": 1}
    (directory / "complete.json").unlink()
    assert service.list()["runs"] == []


@pytest.mark.parametrize("run_id", ["../outside", "lp-xyz", "lp-" + "a" * 20 + "/timeline.json"])
def test_saved_run_ids_cannot_read_arbitrary_paths(saved, run_id):
    with pytest.raises(ValueError, match="run ID"):
        saved[0].load(run_id)


def test_saved_http_routes_use_server_access_checks(saved):
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    from kuru_backtester.server.backtest_server import BacktestHandler
    service, run_id, _, summary, timeline = saved
    handler = type("SavedHandler", (BacktestHandler,), {"saved_backtests": service, "access_token": "test-access"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/api/saved-backtests")
        assert error.value.code == 403
        request = Request(base + "/api/saved-backtests/" + run_id, headers={"Cookie": "kuru_access=test-access"})
        assert json.load(urlopen(request)) == {**summary, "compact_timeline": timeline}
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
