import hashlib
import json

from kuru_backtester.history import promote_historical_partition as promotion
import pytest


def test_promotion_requires_the_validated_event_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(promotion, "ROOT", tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "market": "0xabc", "event_partitions": [], "checkpoints": [],
    }))
    events = tmp_path / "events.jsonl"
    events.write_text('{"block_number":10}\n')
    summary = tmp_path / "summary.json"
    summary.write_text("{}")
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({
        "market": "0xabc", "checkpoint_block": 9, "checkpoint_timestamp": 99,
    }))
    monkeypatch.setattr(
        promotion.strategy,
        "verify_validation_summary",
        lambda path: {
            "interval": {"market": "0xabc", "from_block": 10, "to_block": 20},
            "event_input": {"record_count": 1, "source_sha256": "0" * 64},
        },
    )

    with pytest.raises(ValueError, match="hash does not match"):
        promotion.promote(manifest, events, summary, checkpoint)


def test_promotion_records_bound_event_and_checkpoint_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(promotion, "ROOT", tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "market": "0xabc", "event_partitions": [], "checkpoints": [],
    }))
    events = tmp_path / "events.jsonl"
    events.write_text('{"block_number":10}\n')
    event_hash = hashlib.sha256(events.read_bytes()).hexdigest()
    summary = tmp_path / "summary.json"
    summary.write_text("{}")
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({
        "market": "0xabc", "checkpoint_block": 9, "checkpoint_timestamp": 99,
    }))
    monkeypatch.setattr(
        promotion.strategy,
        "verify_validation_summary",
        lambda path: {
            "interval": {"market": "0xabc", "from_block": 10, "to_block": 20},
            "event_input": {"record_count": 1, "source_sha256": event_hash},
        },
    )

    result = promotion.promote(manifest, events, summary, checkpoint)
    saved = json.loads(manifest.read_text())
    assert result["event_sha256"] == event_hash
    assert saved["event_partitions"][0]["sha256"] == event_hash
    assert saved["checkpoints"][0]["sha256"] == hashlib.sha256(
        checkpoint.read_bytes()
    ).hexdigest()
