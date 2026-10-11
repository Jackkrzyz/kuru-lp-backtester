"""Tests for safe Kuru L2 parsing."""

import math
import unittest

import pandas as pd

from kuru_backtester.legacy.analyze_l2 import best_price, build_metrics_frame


class BestPriceTests(unittest.TestCase):
    def test_converts_raw_1e18_integer_prices(self) -> None:
        bids = '[["2000000000000000000", "4"], ["1500000000000000000", "2"]]'
        asks = '[["2500000000000000000", "1"], ["3000000000000000000", "3"]]'

        self.assertEqual(best_price(bids, "bid"), 2.0)
        self.assertEqual(best_price(asks, "ask"), 2.5)

    def test_accepts_published_decimal_prices(self) -> None:
        bids = '[["0.02230900", "1"], ["0.02230400", "2"]]'

        self.assertEqual(best_price(bids, "bid"), 0.022309)

    def test_empty_and_malformed_books_return_nan(self) -> None:
        self.assertTrue(math.isnan(best_price("[]", "bid")))
        self.assertTrue(math.isnan(best_price("not-json", "ask")))
        self.assertTrue(math.isnan(best_price('[{"size": "1"}]', "bid")))


class MetricsFrameTests(unittest.TestCase):
    def test_calculates_metrics_and_keeps_bad_rows(self) -> None:
        raw = pd.DataFrame(
            {
                "timestamp": [1_700_000_000, "bad"],
                "bids": ['[["2000000000000000000", "1"]]', "not-json"],
                "asks": ['[["2200000000000000000", "1"]]', "[]"],
            }
        )

        result = build_metrics_frame(raw)

        self.assertEqual(
            list(result.columns),
            ["timestamp", "best_bid", "best_ask", "midpoint", "spread", "spread_percent"],
        )
        self.assertAlmostEqual(result.loc[0, "midpoint"], 2.1)
        self.assertAlmostEqual(result.loc[0, "spread"], 0.2)
        self.assertAlmostEqual(result.loc[0, "spread_percent"], 0.2 / 2.1 * 100)
        self.assertTrue(pd.isna(result.loc[1, "timestamp"]))
        self.assertTrue(math.isnan(result.loc[1, "midpoint"]))

    def test_rejects_missing_columns(self) -> None:
        with self.assertRaisesRegex(ValueError, "asks"):
            build_metrics_frame(pd.DataFrame({"timestamp": [], "bids": []}))


if __name__ == "__main__":
    unittest.main()
