import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from monitor import summarize_observations, select_diverse_candidates


class MonitorSummaryTests(unittest.TestCase):
    def test_price_extraction_does_not_claim_inventory(self):
        rows = {
            "price-stock-unknown": {
                "retrieval_status": "OK", "parse_method": "JSON_LD", "final_url": "https://shop.example/item/1",
                "currency": "JPY", "price_jpy": 4500, "availability": "UNKNOWN",
                "auto_discovered_candidate": True,
            },
            "available": {
                "retrieval_status": "OK", "parse_method": "META_PRODUCT", "final_url": "https://shop.example/item/2",
                "currency": "JPY", "price_jpy": 5000, "availability": "IN_STOCK",
            },
            "sold": {
                "retrieval_status": "OK", "parse_method": "JSON_LD", "final_url": "https://shop.example/item/3",
                "currency": "JPY", "price_jpy": 1200, "availability": "OUT_OF_STOCK",
            },
            "error": {
                "retrieval_status": "HTTP_ERROR", "currency": None, "price_jpy": None,
                "availability": "UNKNOWN",
            },
        }
        summary = summarize_observations(rows)
        self.assertEqual(summary["watched_count"], 4)
        self.assertEqual(summary["page_fetch_success_count"], 3)
        self.assertEqual(summary["structured_data_count"], 2)
        self.assertEqual(summary["meta_price_count"], 1)
        self.assertEqual(summary["price_extracted_count"], 3)
        self.assertEqual(summary["in_stock_confirmed_count"], 1)
        self.assertEqual(summary["out_of_stock_confirmed_count"], 1)
        self.assertEqual(summary["stock_unknown_count"], 1)
        self.assertEqual(summary["error_count"], 1)

    def test_page_fetch_without_price_is_not_counted_as_fetch_error(self):
        summary = summarize_observations({
            "no-price": {
                "retrieval_status": "PRICE_NOT_FOUND", "parse_method": None,
                "final_url": "https://shop.example/item/4", "price_jpy": None,
                "currency": None, "availability": "UNKNOWN",
            }
        })
        self.assertEqual(summary["page_fetch_success_count"], 1)
        self.assertEqual(summary["price_extraction_issue_count"], 1)
        self.assertEqual(summary["error_count"], 0)
        self.assertEqual(summary["stock_unknown_count"], 1)

    def test_auto_watch_selection_prefers_distinct_marketplaces(self):
        candidates = [
            {"listing_url":"https://shopping.bookoff.co.jp/used/0012345678","last_seen_at":"2026-10-21T13:00:00+09:00"},
            {"listing_url":"https://shopping.bookoff.co.jp/used/0012345679","last_seen_at":"2026-10-21T13:00:00+09:00"},
            {"listing_url":"https://jp.mercari.com/item/m12345678901","last_seen_at":"2026-10-20T13:00:00+09:00"},
        ]
        selected = select_diverse_candidates(candidates, 2)
        hosts = {(item["listing_url"].split("/")[2]) for item in selected}
        self.assertEqual(len(selected), 2)
        self.assertIn("shopping.bookoff.co.jp", hosts)
        self.assertIn("jp.mercari.com", hosts)

    def test_auto_watch_selection_fills_slots_when_only_one_host_exists(self):
        candidates = [
            {"listing_url":"https://shopping.bookoff.co.jp/used/0012345678","last_seen_at":"2026-10-21T13:00:00+09:00"},
            {"listing_url":"https://shopping.bookoff.co.jp/used/0012345679","last_seen_at":"2026-10-21T12:00:00+09:00"},
            {"listing_url":"https://shopping.bookoff.co.jp/used/0012345680","last_seen_at":"2026-10-21T11:00:00+09:00"},
        ]
        selected = select_diverse_candidates(candidates, 2)
        self.assertEqual(len(selected), 2)
        self.assertNotEqual(selected[0]["listing_url"], selected[1]["listing_url"])

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
