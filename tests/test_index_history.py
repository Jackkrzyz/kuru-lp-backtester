import json

from kuru_backtester.history.index_history import EVENT_TOPICS, LOG_FIELDS, StagingPartition, canonical_page_rows, index_query


def test_index_query_has_canonical_fields_and_filters():
    payload = index_query("0xABC", 10, 20)
    assert payload["logs"] == [{"address": ["0xabc"], "topics": [EVENT_TOPICS]}]
    assert "transaction_index" in LOG_FIELDS
    assert payload["join_mode"] == "Default"


def test_staging_partition_resume_and_deduplicate(tmp_path):
    signature = {"from_block": 10, "to_block": 20}
    store = StagingPartition(tmp_path, signature)
    row = {"block_number": 10, "transaction_hash": "0x1", "log_index": 2}
    assert store.append([row, row]) == 1
    store.state["next_block"] = 11
    store.save()
    resumed = StagingPartition(tmp_path, signature)
    assert resumed.state["next_block"] == 11
    assert resumed.append([row]) == 0
    assert len(tmp_path.joinpath("events.jsonl").read_text().splitlines()) == 1


def test_page_rows_use_transaction_order_and_timestamp_map():
    topic = EVENT_TOPICS[0]
    result = {"data": [{
        "blocks": [{"number": 12, "timestamp": "0x64"}],
        "logs": [
            {"address": "0xabc", "block_number": 12, "transaction_hash": "0x2", "transaction_index": 3, "log_index": 9, "topic0": topic, "data": "0x"},
            {"address": "0xabc", "block_number": 12, "transaction_hash": "0x1", "transaction_index": 1, "log_index": 4, "topic0": topic, "data": "0x"},
        ],
    }]}
    rows = canonical_page_rows(result, "0xabc", lambda block: 0)
    assert [item["transaction_index"] for item in rows] == [1, 3]
    assert rows[0]["block_timestamp"] == 100


def test_commit_telemetry_does_not_change_event_file_or_cursor(tmp_path):
    from kuru_backtester.history.index_history import run_index
    from kuru_backtester.history.discover_envio_intervals import RateLimitedHyperSyncClient
    from kuru_backtester.engine.rebuild_from_chain import EVENT_SPECS
    spec = EVENT_SPECS[0]
    response = {"next_block": 20, "data": [{"blocks": [{"number": 12, "timestamp": 100}], "logs": [
        {"address": "0xabc", "block_number": 12, "transaction_hash": "0x1", "transaction_index": 0, "log_index": 0, "topic0": spec.topic, "data": "0x"}]}]}
    traces, paths = [], []
    for label, observer in (("plain", None), ("trace", traces.append)):
        client = RateLimitedHyperSyncClient("test://source", "test-token", http_post=lambda payload: (response, {}), observer=observer)
        partition = StagingPartition(tmp_path / label, {"from_block": 10, "to_block": 19})
        run_index(client, partition, "0xabc", 20)
        paths.append(partition.events_path)
        assert partition.state["next_block"] == 20
    assert paths[0].read_bytes() == paths[1].read_bytes()
    commit = next(item for item in traces if item["kind"] == "page_committed")
    assert commit["from_block"] == 10
    assert commit["next_block"] == 20
    assert commit["event_counts"] == {spec.name: 1}
    assert commit["new_events"] == 1
