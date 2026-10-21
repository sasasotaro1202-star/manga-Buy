import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from report import candidate_display_title, candidate_observation_sort_key


class ReportCandidateTests(unittest.TestCase):
    def test_actual_product_title_fills_missing_search_result_title(self):
        item = {"listing_url":"https://shopping.bookoff.co.jp/used/0012345678","search_result_title":"","source_domain":"shopping.bookoff.co.jp"}
        observed = {"title":"Example 4巻","retrieval_status":"OK"}
        self.assertEqual(candidate_display_title(item, observed), "Example 4巻")

    def test_in_stock_candidates_sort_before_unknown_and_sold_out(self):
        observations = {
            "https://shop.example/a": {"availability":"UNKNOWN","currency":"JPY","price_jpy":100,"retrieval_status":"OK"},
            "https://shop.example/b": {"availability":"IN_STOCK","currency":"JPY","price_jpy":900,"retrieval_status":"OK"},
            "https://shop.example/c": {"availability":"OUT_OF_STOCK","currency":"JPY","price_jpy":10,"retrieval_status":"OK"},
        }
        items = [{"listing_url":url} for url in observations]
        ordered = sorted(items,key=lambda item:candidate_observation_sort_key(item,observations))
        self.assertEqual([x["listing_url"] for x in ordered],[
            "https://shop.example/b","https://shop.example/a","https://shop.example/c"
        ])


if __name__ == "__main__":
    unittest.main()
