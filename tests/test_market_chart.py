"""Local chart indexing does not reconstruct books or contact the network."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from eth_abi import encode
import pytest

from kuru_backtester.server import market_chart as chart
from kuru_backtester.engine.rebuild_from_chain import EVENT_SPECS, MARKET_ADDRESS


def trade(block, stamp, price, size=10**10, index=0):
    owner = "0x" + "11" * 20
    spec = next(spec for spec in EVENT_SPECS if spec.name == "Trade")
    return {"address": MARKET_ADDRESS, "block_number": block, "block_timestamp": stamp,
            "transaction_hash": "0x" + f"{block:064x}", "transaction_index": 0,
            "log_index": index, "topic0": spec.topic,
            "data": "0x" + encode(spec.types, (1, owner, True, price * 10**18, 0, owner, owner, size)).hex()}


def source(tmp_path, name, rows):
    path = tmp_path / name
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def test_trade_only_ohlc_uses_chain_order_and_preserves_empty_minutes(tmp_path):
    rows = [trade(13, 3604, 4), trade(11, 3602, 5), trade(12, 3603, 2),
            trade(10, 3601, 3), trade(14, 3780, 6), {"midpoint": 9999}]
    bars = chart.read_trades(source(tmp_path, "events.jsonl", rows), [(10, 14)], threading.Event())
    assert len(bars) == 2
    first = next(bar for bar in bars if bar["time"] == 3600)
    assert [first[key] // 10**18 for key in ("open", "high", "low", "close")] == [3, 5, 2, 4]
    assert first["volume_raw"] == 4 * 10**10
    assert first["trades"] == 4
    assert {bar["time"] for bar in bars} == {3600, 3780}


def test_overlapping_sources_are_counted_once_and_cache_is_resumable(tmp_path, monkeypatch):
    a = source(tmp_path, "a.jsonl", [trade(10, 3600, 1), trade(11, 3601, 2)])
    b = source(tmp_path, "b.jsonl", [trade(11, 3601, 2), trade(12, 3602, 3)])
    monkeypatch.setattr(chart.MarketChart, "sources", lambda self: iter([(a, 10, 11), (b, 11, 12)]))
    first = chart.MarketChart(tmp_path)
    first.build()
    assert first.state["phase"] == "complete"
    assert sum(bar["trades"] for bar in first.bars) == 3
    assert first.coverage == [(10, 12)]
    monkeypatch.setattr(chart, "read_trades", lambda *args: pytest.fail("Must reuse local candle cache"))
    again = chart.MarketChart(tmp_path)
    again.build()
    assert again.bars == first.bars


def test_changed_source_invalidates_its_contribution(tmp_path, monkeypatch):
    path = source(tmp_path, "events.jsonl", [trade(10, 3600, 1)])
    monkeypatch.setattr(chart.MarketChart, "sources", lambda self: iter([(path, 10, 11)]))
    index = chart.MarketChart(tmp_path)
    index.build()
    source(tmp_path, "events.jsonl", [trade(10, 3600, 1), trade(11, 3601, 2)])
    index.build()
    assert sum(bar["trades"] for bar in index.bars) == 2


def test_large_contiguous_archive_coverage_stays_compact_and_keeps_gaps():
    coverage = [(20000, 20001)]
    for block in range(10000):
        assert chart.subtract(block, block, coverage) == [(block, block)]
        coverage = chart.merge_coverage([*coverage, (block, block)])
        assert len(coverage) <= 2
    assert coverage == [(0, 9999), (20000, 20001)]
    assert chart.subtract(0, 20001, coverage) == [(10000, 19999)]


def test_refresh_keeps_previous_chart_until_replacement_is_complete(tmp_path, monkeypatch):
    a = source(tmp_path, "a.jsonl", [trade(10, 3600, 1)])
    b = source(tmp_path, "b.jsonl", [trade(20, 7200, 2)])
    index = chart.MarketChart(tmp_path)
    monkeypatch.setattr(index, "sources", lambda: iter([(a, 10, 10), (b, 20, 20)]))
    index.build()
    before = index.bars
    def refreshing():
        yield a, 10, 10
        assert index.bars == before, "The first refresh partition must not replace the entire chart"
        yield b, 20, 20
    monkeypatch.setattr(index, "sources", refreshing)
    index.build()
    assert index.bars == before


def test_foreign_or_broken_trade_does_not_claim_chart_coverage(tmp_path, monkeypatch):
    row = trade(10, 3600, 1)
    row["address"] = "0x" + "22" * 20
    path = source(tmp_path, "events.jsonl", [row])
    monkeypatch.setattr(chart.MarketChart, "sources", lambda self: iter([(path, 10, 10)]))
    index = chart.MarketChart(tmp_path)
    index.build()
    assert index.state["phase"] == "failed"
    assert index.coverage == []
    assert not list((tmp_path / ".cache/market_chart").glob("*.json"))


def test_snapshot_is_bounded_and_retains_raw_volume(tmp_path):
    index = chart.MarketChart(tmp_path)
    index.next_refresh = time.monotonic() + 100
    index.bars = [{"time": i * 60, "open": 10**18, "high": 2 * 10**18,
                   "low": 10**18, "close": 2 * 10**18, "volume_raw": 10**10, "trades": 1,
                   "first_position": [i, 0, 0], "last_position": [i, 0, 0]} for i in range(4000)]
    output = index.snapshot()
    assert len(output["candles"]) <= 1500
    assert sum(int(bar["volume_raw"]) for bar in output["candles"]) == 4000 * 10**10
    assert output["candles"][0]["open"] == 1
    assert output["candles"][-1]["close"] == 2
    assert output["candles"][-1]["to_block"] == 3999
    zoom = index.snapshot(60, 120, 60)
    assert len(zoom["candles"]) == 2
    assert zoom["candle_seconds"] == 60


def test_empty_blocks_are_chart_coverage_not_fake_trades(tmp_path, monkeypatch):
    path = source(tmp_path, "events.jsonl", [{"midpoint": 999}])
    monkeypatch.setattr(chart.MarketChart, "sources", lambda self: iter([(path, 10, 20)]))
    index = chart.MarketChart(tmp_path)
    index.build()
    assert index.coverage == [(10, 20)]
    assert index.bars == []


def test_stop_interrupts_reader_without_committing_page(tmp_path):
    path = source(tmp_path, "events.jsonl", [trade(10, 3600, 1)])
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(InterruptedError):
        chart.read_trades(path, [(10, 10)], stopped)


def test_range_subtraction_preserves_inclusive_edges():
    assert chart.subtract(10, 30, [(10, 15), (14, 20), (25, 29)]) == [(21, 24), (30, 30)]


def test_default_daily_candle_and_zoom_keep_entire_utc_day(tmp_path):
    index = chart.MarketChart(tmp_path)
    index.next_refresh = time.monotonic() + 100
    rows = [trade(10, 86401, 4), trade(11, 100000, 6), trade(12, 172790, 3), trade(13, 172801, 5)]
    index.bars = chart.read_trades(source(tmp_path, "events.jsonl", rows), [(10, 13)], threading.Event())
    full = index.snapshot()
    assert full["candle_seconds"] == 86400
    assert [bar["time"] for bar in full["candles"]] == [86400, 172800]
    zoom = index.snapshot(100001, 100020)
    assert zoom["candles"] == full["candles"][:1]
    assert [zoom["candles"][0][key] for key in ("open", "high", "low", "close")] == [4, 6, 3, 3]
    with pytest.raises(ValueError):
        index.snapshot(seconds=0)


def test_weekly_candles_start_monday_and_include_entire_week(tmp_path):
    index = chart.MarketChart(tmp_path)
    index.next_refresh = time.monotonic() + 100
    monday = 345600  # 1970-01-05 00:00 UTC
    rows = [trade(10, monday - 1, 2), trade(11, monday, 4),
            trade(12, monday + 300000, 6), trade(13, monday + 604799, 3),
            trade(14, monday + 604800, 5)]
    index.bars = chart.read_trades(source(tmp_path, "weeks.jsonl", rows), [(10, 14)], threading.Event())
    full = index.snapshot(seconds=604800)
    assert full["candle_seconds"] == 604800
    assert [bar["time"] for bar in full["candles"]] == [monday - 604800, monday, monday + 604800]
    zoom = index.snapshot(monday + 100000, monday + 100001, seconds=604800)
    assert zoom["candles"] == full["candles"][1:2]
    bar = zoom["candles"][0]
    assert [bar[key] for key in ("open", "high", "low", "close")] == [4, 6, 3, 3]
    assert bar["trades"] == 3
    assert bar["volume_raw"] == str(3 * 10**10)
    assert bar["volume_mon"] == 3
    assert (bar["from_block"], bar["to_block"]) == (11, 13)


def test_weekly_overview_matches_complete_trade_index(tmp_path, monkeypatch):
    monday = 345600
    rows = [trade(10, monday, 4), trade(11, monday + 86400, 7),
            trade(12, monday + 604799, 2), trade(13, monday + 3 * 604800, 5)]
    path = source(tmp_path, "weeks.jsonl", rows)
    monkeypatch.setattr(chart.MarketChart, "sources", lambda self: iter([(path, 10, 13)]))
    index = chart.MarketChart(tmp_path)
    index.build()
    expected = index.snapshot(seconds=604800)
    restarted = chart.MarketChart(tmp_path)
    restarted.next_refresh = time.monotonic() + 100
    saved = restarted.snapshot(seconds=604800)
    assert saved["saved_overview"] is True
    assert saved["candles"] == expected["candles"]
    assert [bar["time"] for bar in saved["candles"]] == [monday, monday + 3 * 604800]
    zoom = restarted.snapshot(monday + 86401, monday + 86402, seconds=604800)
    assert zoom["candles"] == expected["candles"][:1]


def test_concurrent_chart_builds_publish_complete_cache_without_temp_collision(tmp_path, monkeypatch):
    path = source(tmp_path, "events.jsonl", [trade(10, 3600, 1), trade(11, 3601, 2)])
    monkeypatch.setattr(chart.MarketChart, "sources", lambda self: iter([(path, 10, 11)]))
    ready = threading.Barrier(2)
    rename = threading.Barrier(2)
    original_read, original_replace = chart.read_trades, chart.os.replace
    temporary_names = []
    def read(*args):
        ready.wait(timeout=5)
        return original_read(*args)
    def replace(source, destination):
        temporary_names.append(str(source))
        rename.wait(timeout=5)
        return original_replace(source, destination)
    monkeypatch.setattr(chart, "read_trades", read)
    monkeypatch.setattr(chart.os, "replace", replace)
    indexes = [chart.MarketChart(tmp_path), chart.MarketChart(tmp_path)]
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda index: index.build(), indexes))
    assert all(index.state["phase"] == "complete" for index in indexes)
    assert indexes[0].bars == indexes[1].bars
    assert len(set(temporary_names)) == 4  # contribution and overview, both writers
    caches = [path for path in indexes[0].directory.glob("*.json") if path.name != "overview.json"]
    assert len(caches) == 1
    assert json.loads(caches[0].read_text()) == indexes[0].bars
    assert not list(indexes[0].directory.glob("*.tmp"))


def test_failed_cache_publish_preserves_previous_cache_and_cleans_own_temp(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    chart.write_cache(path, {"old": True})
    def fail(*args):
        raise OSError("rename failed")
    monkeypatch.setattr(chart.os, "replace", fail)
    with pytest.raises(OSError, match="rename failed"):
        chart.write_cache(path, {"new": True})
    assert json.loads(path.read_text()) == {"old": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_repeated_snapshots_reuse_candles_but_keep_current_job_state(tmp_path, monkeypatch):
    index = chart.MarketChart(tmp_path)
    index.next_refresh = time.monotonic() + 100
    index.bars = chart.read_trades(source(tmp_path, "events.jsonl", [trade(10, 86401, 1)]), [(10, 10)], threading.Event())
    first = index.snapshot()
    merge = chart.merge_bars
    monkeypatch.setattr(chart, "merge_bars", lambda *args: pytest.fail("Unchanged candles must not be rebuilt for each poll"))
    index.state = {"phase": "complete", "processed_files": 42}
    repeated = index.snapshot()
    assert repeated["candles"] is first["candles"]
    assert repeated["state"]["processed_files"] == 42
    monkeypatch.setattr(chart, "merge_bars", merge)
    index.bars = [*index.bars, {**index.bars[0], "time": 172800}]
    changed = index.snapshot()
    assert len(changed["candles"]) == 2
    assert len(index.snapshot_cache) == 1


def test_saved_daily_overview_is_available_during_restart_refresh(tmp_path, monkeypatch):
    path = source(tmp_path, "events.jsonl", [trade(10, 86401, 1)])
    monkeypatch.setattr(chart.MarketChart, "sources", lambda self: iter([(path, 10, 10)]))
    first = chart.MarketChart(tmp_path)
    first.build()
    restarted = chart.MarketChart(tmp_path)
    restarted.next_refresh = time.monotonic() + 100
    saved = restarted.snapshot()
    assert saved["saved_overview"] is True
    assert saved["candles"] == first.snapshot()["candles"]
    assert restarted.snapshot(seconds=60)["candles"] == [], "A daily overview must not be presented as minute data"
    restarted.build()
    assert "saved_overview" not in restarted.snapshot()


def test_invalid_or_other_market_overview_is_ignored(tmp_path):
    directory = tmp_path / ".cache/market_chart"
    directory.mkdir(parents=True)
    for value in ({"schema_version": 0}, {"schema_version": 1, "market": "other"},
                  {"schema_version": 1, "market": MARKET_ADDRESS}):
        (directory / "overview.json").write_text(json.dumps(value))
        assert chart.MarketChart(tmp_path).overview is None


def test_manual_trade_refresh_is_async_and_has_only_one_reader(tmp_path, monkeypatch):
    index=chart.MarketChart(tmp_path)
    entered,release=threading.Event(),threading.Event()
    def build():
        entered.set()
        assert release.wait(3)
    monkeypatch.setattr(index,"build",build)
    try:
        assert index.request_refresh()["already_running"] is False
        assert entered.wait(1)
        assert index.request_refresh()["already_running"] is True
    finally:
        release.set();index.thread.join(2);index.close()
