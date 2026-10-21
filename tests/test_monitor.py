import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from monitor import summarize_observations


class MonitorSummaryTests(unittest.TestCase):
    def test_price_extraction_does_not_claim_inventory(self):
        rows = {
            "price-stock-unknown": {
                "retrieval_status": "OK", "currency": "JPY", "price_jpy": 4500,
                "availability": "UNKNOWN", "auto_discovered_candidate": True,
            },
            "available": {
                "retrieval_status": "OK", "currency": "JPY", "price_jpy": 5000,
                "availability": "IN_STOCK",
            },
            "sold": {
                "retrieval_status": "OK", "currency": "JPY", "price_jpy": 1200,
                "availability": "OUT_OF_STOCK",
            },
            "error": {
                "retrieval_status": "HTTP_ERROR", "currency": None, "price_jpy": None,
                "availability": "UNKNOWN",
            },
        }
        summary = summarize_observations(rows)
        self.assertEqual(summary["watched_count"], 4)
        self.assertEqual(summary["structured_data_count"], 3)
        self.assertEqual(summary["price_extracted_count"], 3)
        self.assertEqual(summary["in_stock_confirmed_count"], 1)
        self.assertEqual(summary["out_of_stock_confirmed_count"], 1)
        self.assertEqual(summary["stock_unknown_count"], 2)
        self.assertEqual(summary["error_count"], 1)

    def test_currency_unknown_or_boolean_price_is_not_counted(self):
        rows = {
            "unknown-currency": {
                "retrieval_status": "OK", "currency": None, "price_jpy": 4500,
                "availability": "UNKNOWN",
            },
            "boolean-price": {
                "retrieval_status": "OK", "currency": "JPY", "price_jpy": True,
                "availability": "IN_STOCK",
            },
        }
        summary = summarize_observations(rows)
        self.assertEqual(summary["price_extracted_count"], 0)
        self.assertEqual(summary["in_stock_confirmed_count"], 1)


if __name__ == "__main__":
    unittest.main()
