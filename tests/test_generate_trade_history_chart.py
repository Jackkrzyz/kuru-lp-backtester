import json

from kuru_backtester.tools.generate_trade_history_chart import build_chart, downsample_trades


def test_downsample_is_bounded_and_keeps_ends():
    rows = [
        {"block": index, "timestamp": index, "price": float(index % 9), "volume_raw": "1", "ordinal": index}
        for index in range(200)
    ]
    result = downsample_trades(rows, 40)
    assert len(result) <= 40
    assert result[0]["block"] == 0
    assert result[-1]["block"] >= 180


def test_chart_separates_trade_scope_from_l3_coverage(tmp_path):
    trade_path = tmp_path / "trades.jsonl"
    trade_path.write_text("\n".join(
        json.dumps({
            "name": "Trade", "block_number": block, "block_timestamp": block + 10,
            "trade_price": 1 + block / 100, "trade_size": 5,
        }) for block in range(10, 20)
    ) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "market": "0xabc",
        "trade_chart_coverage": {"from_block": 10, "to_block": 19, "path": "trades.jsonl"},
        "event_partitions": [{"from_block": 12, "to_block": 15}],
    }))
    output = tmp_path / "chart.json"
    result = build_chart(manifest, output, 8)
    assert result["complete_lifetime"] is False
    assert result["trade_only_coverage"]["from_block"] == 10
    assert result["validated_l3_coverage"] == [{
        "from_block": 12, "to_block": 15, "complete_validated_l3": True,
    }]
    assert json.loads(output.read_text())["point_count"] <= 8
