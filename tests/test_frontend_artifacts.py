"""Contract tests for the static frontend and generated replay files."""

import json
import unittest
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests" / "fixtures" / "legacy_replays"


class FrontendArtifactTests(unittest.TestCase):
    def test_data_diagram_and_trace_share_one_panel(self) -> None:
        class PanelParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.sections = []
                self.panels = []
                self.ids = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "section":
                    panel = {"classes": attrs.get("class", "").split(), "ids": []}
                    self.sections.append(panel)
                    self.panels.append(panel)
                if "id" in attrs:
                    self.ids.append(attrs["id"])
                    for panel in self.sections:
                        panel["ids"].append(attrs["id"])

            def handle_endtag(self, tag):
                if tag == "section":
                    self.sections.pop()

        parser = PanelParser()
        parser.feed((ROOT / "frontend" / "indexing.html").read_text(encoding="utf-8"))
        panels = [p for p in parser.panels if "instrument" in p["classes"]]
        self.assertEqual(len(panels), 1)
        self.assertIn("chain-map", panels[0]["classes"])
        for element in ("flow-title", "current-action", "action-explanation", "action-trace",
                        "downloaded", "reconstructed", "validated", "coverage", "page-inspector"):
            self.assertIn(element, panels[0]["ids"])
        self.assertEqual(len(parser.ids), len(set(parser.ids)), "Duplicate IDs break status updates")

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = json.loads((DATA / "catalog.json").read_text(encoding="utf-8"))
        cls.lp_catalog = json.loads((DATA / "lp_catalog.json").read_text(encoding="utf-8"))

    def test_default_is_active_and_conservatively_capped(self) -> None:
        self.assertEqual(self.catalog["default"]["interval"], "relatively_active")
        self.assertEqual(self.catalog["default"]["scenario"], "max_10_percent")

    def test_discovery_and_order_enrichment_scopes_are_separate(self) -> None:
        scope = self.catalog["data_scope"]
        self.assertIn("broad_trade_discovery", scope)
        self.assertIn("candidate_order_enrichment", scope)
        self.assertEqual(scope["broad_trade_discovery"]["full_scan_run"], False)
        self.assertEqual(
            set(scope["candidate_order_enrichment"]["intervals"]),
            {"relatively_quiet", "relatively_active", "directional"},
        )

    def test_all_replays_fit_limit_and_match_raw_summaries(self) -> None:
        if not all((DATA / item["path"]).is_file() for item in self.catalog["artifacts"]):
            self.skipTest("optional archived demo replays are not present locally")
        self.assertEqual(len(self.catalog["artifacts"]), 12)
        for artifact in self.catalog["artifacts"]:
            with self.subTest(interval=artifact["interval"], scenario=artifact["scenario"]):
                replay = json.loads((DATA / artifact["path"]).read_text(encoding="utf-8"))
                summary_path = ROOT / artifact["raw_path"].replace("timeline.jsonl", "summary.json")
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                self.assertLessEqual(len(replay["frames"]), 1_500)
                self.assertEqual(len(replay["frames"]), artifact["compact_frame_count"])
                self.assertEqual(replay["frames"][0]["sequence"], 1)
                final = replay["frames"][-1]
                expected = summary["metrics"]
                for actual_value, expected_value in (
                    (final["cash"], expected["final_cash"]),
                    (final["inventory"], expected["final_inventory"]),
                    (final["marked_value"], expected["final_portfolio_value"]),
                    (final["cumulative_pnl"], expected["net_pnl"]),
                    (final["hold_baseline_pnl"], summary["hold_baseline"]["hold_pnl"]),
                ):
                    self.assertEqual(Decimal(str(actual_value)), Decimal(str(expected_value)))
                counts = replay["compaction"]["action_counts"]
                self.assertEqual(counts["placements"], expected["placement_count"])
                self.assertEqual(counts["cancellations"], expected["cancellation_count"])
                self.assertEqual(counts["fills"], expected["fill_count"])
                self.assertLess(artifact["compact_size_bytes"], artifact["raw_size_bytes"])

    def test_frontend_contains_required_controls(self) -> None:
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        for control in (
            "market",
            "from-block",
            "to-block",
            "liquidity-shape",
            "mon-amount",
            "usdc-amount",
            "play-button",
            "scrubber",
        ):
            self.assertIn(f'id="{control}"', html)
        for speed in ("1", "10", "50", "100"):
            self.assertIn(f'data-speed="{speed}"', html)
        self.assertIn("requestAnimationFrame", javascript)
        self.assertNotIn("WebSocket", javascript)

    def test_kuru_lp_is_default_and_has_exact_sdk_preview_controls(self) -> None:
        self.assertEqual(self.lp_catalog["default"]["engine"], "kuru_lp")
        self.assertEqual(self.lp_catalog["default"]["interval"], "relatively_active")
        self.assertEqual(self.lp_catalog["default"]["shape"], "spot")
        self.assertEqual(self.lp_catalog["default"]["scenario"], "max_10_percent")
        self.assertEqual(self.lp_catalog["sdk_reference"]["version"], "0.0.95")
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        for control in (
            "liquidity-shape",
            "lower-range-percent",
            "upper-range-percent",
            "absolute-range-mode",
            "minimum-price",
            "maximum-price",
            "flip-spread",
            "mon-amount",
            "usdc-amount",
            "minimum-mon",
            "minimum-usdc",
            "distribution-chart",
            "smallest-order",
            "market-minimum-order",
            "invalid-order-count",
            "offending-order",
            "deployable-status",
        ):
            self.assertIn(f'id="{control}"', html)
        self.assertNotIn("Advanced MM Demo", html)
        self.assertNotIn("Advanced simulation", html)
        self.assertNotIn("catalog.json", javascript)
        self.assertNotIn("loadDemoReplay", javascript)
        self.assertIn("KuruSdkAdapter.generateOrderGrid", javascript)
        self.assertIn("KuruSdkAdapter.percentagePriceBounds", javascript)
        self.assertIn("KuruSdkAdapter.validateOrderGrid", javascript)
        self.assertNotIn('id="auto-fill-amounts"', html)
        self.assertNotIn('id="use-minimum-amounts"', html)
        self.assertIn('amountDriver: "both"', javascript)
        self.assertIn("KuruSdkAdapter.minimumDepositSummary", javascript)
        self.assertIn("KuruSdkAdapter.serializeOrderGrid", javascript)
        self.assertIn('fetch("/api/backtests"', javascript)
        self.assertIn("pollBacktestJob", javascript)
        self.assertIn("/result`,", javascript)
        self.assertIn('method: "DELETE"', javascript)
        self.assertIn("active_order_deltas", javascript)
        self.assertIn("chartLayer", javascript)
        self.assertNotIn("strategy_output", javascript)
        self.assertNotIn("multi_regime_output", javascript)
        self.assertNotIn("function sdkGrid", javascript)
        self.assertNotIn("lpCatalog", javascript)
        self.assertNotIn("lp_catalog.json", javascript)
        self.assertNotIn("sample", javascript.lower())
        self.assertIn('type="number" min="0.01" step="0.01" value="0.30"', html)
        self.assertIn("kuru_sdk_adapter.js", html)

    def test_lp_compact_replay_matches_raw_financial_result(self) -> None:
        artifact = self.lp_catalog["artifacts"][0]
        if not (DATA / artifact["path"]).is_file():
            self.skipTest("optional archived demo replay is not present locally")
        replay = json.loads((DATA / artifact["path"]).read_text(encoding="utf-8"))
        summary = json.loads(
            (ROOT / "lp_output" / "relatively_active" / "spot_max_10_percent" / "summary.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(replay["engine"], "kuru_lp")
        self.assertLessEqual(len(replay["frames"]), 1_500)
        final = replay["frames"][-1]
        for actual, expected in (
            (final["cash"], summary["metrics"]["final_cash"]),
            (final["inventory"], summary["metrics"]["final_inventory"]),
            (final["marked_value"], summary["metrics"]["final_portfolio_value"]),
            (final["cumulative_pnl"], summary["metrics"]["position_pnl"]),
        ):
            self.assertEqual(Decimal(str(actual)), Decimal(str(expected)))
        self.assertEqual(
            sum(len(frame["fills"]) for frame in replay["frames"]),
            summary["metrics"]["fill_count"],
        )
        self.assertEqual(summary["metrics"]["cancellation_count"], 0)


if __name__ == "__main__":
    unittest.main()
