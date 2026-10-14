"""Release checks for the committed static demo. No credentials or network."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from kuru_backtester.server.backtest_service import canonical_bytes, resolve_node
from tools.export_replay_demo import export, renderer_source, DEFAULT_RUNS

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "replay-demo"


def test_recorded_files_match_catalog_and_final_financial_state():
    catalog = json.loads((DEMO / "data/catalog.json").read_text())
    assert tuple(row["run_id"] for row in catalog["recordings"]) == DEFAULT_RUNS
    for record in catalog["recordings"]:
        data = (DEMO / record["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == record["sha256"]
        run = json.loads(data)
        assert run["normalized_configuration"] == record["configuration"]
        assert run["results"] == record["results"]
        assert run["interval"] == record["interval"]
        frames = run["compact_timeline"]
        assert hashlib.sha256(canonical_bytes(frames)).hexdigest() == run["timeline_sha256"]
        assert len(frames) == record["frames"] <= 1500
        assert sum(len(frame["fills"]) for frame in frames) == run["fills"]
        assert frames[-1]["cash"] == run["final_usdc"]
        assert frames[-1]["inventory"] == run["final_mon"]
        assert frames[-1]["cumulative_pnl"] == run["net_pnl"]
        assert record["verification"]["status"] == "pass"


def test_demo_copies_current_rendering_without_changing_full_app():
    source = (ROOT / "frontend/app.js").read_text()
    assert (DEMO / "assets/replay-renderer.js").read_text() == renderer_source(source)
    assert (DEMO / "assets/base.css").read_bytes() == (ROOT / "frontend/styles.css").read_bytes()
    assert (DEMO / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()
    assert (DEMO / "assets/kurulabnobg.png").read_bytes() == (ROOT / "Kurulabnobg.png").read_bytes()
    assert 'href="assets/kurulabnobg.png"' in (DEMO / "index.html").read_text()


def test_static_bundle_excludes_servers_credentials_and_private_metadata():
    files = list(DEMO.rglob("*"))
    assert not any(path.suffix in {".py", ".jsonl", ".log"} or path.name.startswith(".env") for path in files)
    assert sum(path.stat().st_size for path in files if path.is_file()) < 7_000_000
    for path in files:
        if path.is_file() and path.suffix != ".png":
            text = path.read_text()
            assert "/api/" not in text
            assert "owner_token" not in text
            assert "ENVIO_API_TOKEN" not in text
            assert "/mnt/c/" not in text
    config = json.loads((DEMO / "vercel.json").read_text())
    assert config["framework"] is None
    assert config["buildCommand"] == config["installCommand"] == ""
    assert config["outputDirectory"] == "."
    assert not {"functions", "crons", "builds", "rewrites"}.intersection(config)


def test_failed_export_never_writes_placeholder_recording(tmp_path):
    (tmp_path / "historical_index_manifest.json").write_text(json.dumps({
        "market": "0x065c9d28e428a0db40191a54d33d5b7c71a9c394", "event_partitions": [], "checkpoints": []}))
    with pytest.raises((FileNotFoundError, ValueError)):
        export([DEFAULT_RUNS[0]], root=tmp_path)
    assert not (tmp_path / "replay-demo").exists()


def test_static_frontend_loads_real_recordings_and_handles_missing_files():
    result = subprocess.run([resolve_node(), "test_replay_demo.js"], cwd=ROOT / "sdk_reference",
                            capture_output=True, text=True, check=True)
    assert "3 real recordings" in result.stdout
