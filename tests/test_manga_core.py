import unittest
import sys
from pathlib import Path
from datetime import timedelta
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from manga_core import (
    now_jst, listing_decision, ranges_cover_all, normalized_ranges,
    minimum_cost_cover, extract_product_data
)


def sample_manga(status="CURATED_BASELINE"):
    return {
        "id": "sample",
        "title": "サンプル漫画",
        "volume_count_status": status,
        "expected_volume_count": 4 if status != "NEEDS_REVIEW" else None,
        "provisional_volume_count": 4,
    }


def sample_listing(**changes):
    now = now_jst().isoformat()
    base = {
        "id": "listing-1",
        "manga_id": "sample",
        "listing_url": "https://jp.mercari.com/item/m123",
        "seller": "seller",
        "included_ranges": [[1, 4]],
        "title_match": "EXACT",
        "edition_match": "EXACT",
        "condition": "GOOD",
        "damage_flags": [],
        "shipping_jpy": 300,
        "mandatory_fees_jpy": 0,
        "costs_checked_at": now,
    }
    base.update(changes)
    return base


def sample_observation(**changes):
    now = now_jst().isoformat()
    base = {
        "listing_id": "listing-1",
        "listing_url": "https://jp.mercari.com/item/m123",
        "retrieval_status": "OK",
        "price_jpy": 1000,
        "currency": "JPY",
        "availability": "IN_STOCK",
        "retrieved_at": now,
    }
    base.update(changes)
    return base


POLICY = {
    "accepted_conditions": ["GOOD", "ACCEPTABLE", "RENTAL_USED"],
    "blocking_damage_flags": ["WATER_DAMAGE", "MOLD", "MISSING_PAGES", "UNREADABLE", "SEVERE_TEAR", "HEAVY_WRITING", "STRONG_ODOR"],
}


class MangaCoreTests(unittest.TestCase):
    def test_valid_ranges_and_coverage(self):
        ranges = normalized_ranges([[1, 2], [3, 4]])
        self.assertEqual(ranges, [(1, 2), (3, 4)])
        self.assertTrue(ranges_cover_all(ranges, 4))
        self.assertFalse(ranges_cover_all([(1, 3)], 4))

    def test_overlapping_ranges_are_rejected(self):
        self.assertIsNone(normalized_ranges([[1, 3], [3, 4]]))
        self.assertIsNone(normalized_ranges([[0, 2]]))
        self.assertIsNone(normalized_ranges([[1, "4"]]))

    def test_confirmed_total_requires_known_shipping_and_fees(self):
        decision = listing_decision(sample_listing(), sample_observation(), sample_manga(), POLICY)
        self.assertEqual(decision["status"], "CONFIRMED")
        self.assertEqual(decision["total_jpy"], 1300)

    def test_unknown_shipping_is_not_zero(self):
        decision = listing_decision(sample_listing(shipping_jpy=None), sample_observation(), sample_manga(), POLICY)
        self.assertEqual(decision["status"], "NOT_ELIGIBLE")
        self.assertIsNone(decision["total_jpy"])
        self.assertIn("SHIPPING_UNKNOWN", decision["reasons"])

    def test_bad_condition_is_excluded(self):
        decision = listing_decision(sample_listing(condition="POOR"), sample_observation(), sample_manga(), POLICY)
        self.assertEqual(decision["status"], "NOT_ELIGIBLE")
        self.assertIn("CONDITION_NOT_ACCEPTABLE_OR_UNKNOWN", decision["reasons"])

    def test_blocking_damage_is_excluded_even_if_condition_says_good(self):
        decision = listing_decision(sample_listing(damage_flags=["MOLD"]), sample_observation(), sample_manga(), POLICY)
        self.assertEqual(decision["status"], "NOT_ELIGIBLE")
        self.assertTrue(any(reason.startswith("BLOCKING_DAMAGE_FLAG") for reason in decision["reasons"]))

    def test_partial_set_is_not_a_full_set(self):
        decision = listing_decision(sample_listing(included_ranges=[[1, 3]]), sample_observation(), sample_manga(), POLICY)
        self.assertEqual(decision["status"], "NOT_ELIGIBLE")
        self.assertIn("NOT_ALL_REQUIRED_VOLUMES_COVERED", decision["reasons"])

    def test_verified_partial_set_can_enter_basket_optimizer_only(self):
        partial = sample_listing(included_ranges=[[1, 2]])
        full_result = listing_decision(partial, sample_observation(), sample_manga(), POLICY)
        self.assertEqual(full_result["status"], "NOT_ELIGIBLE")
        partial_result = listing_decision(
            partial, sample_observation(), sample_manga(), POLICY, require_complete=False
        )
        self.assertEqual(partial_result["status"], "CONFIRMED")
        self.assertEqual(partial_result["included_ranges"], [(1, 2)])
        self.assertNotIn("NOT_ALL_REQUIRED_VOLUMES_COVERED", partial_result["reasons"])

    def test_stale_price_is_excluded(self):
        old = (now_jst() - timedelta(hours=30)).isoformat()
        decision = listing_decision(sample_listing(), sample_observation(retrieved_at=old), sample_manga(), POLICY)
        self.assertEqual(decision["status"], "NOT_ELIGIBLE")
        self.assertIn("PRICE_STALE", decision["reasons"])

    def test_unconfirmed_count_can_only_be_provisional(self):
        decision = listing_decision(sample_listing(), sample_observation(), sample_manga("NEEDS_REVIEW"), POLICY)
        self.assertEqual(decision["status"], "PROVISIONAL")
        self.assertIn("VOLUME_COUNT_NOT_SOURCE_CONFIRMED", decision["reasons"])

    def test_minimum_cost_cover_uses_known_complete_candidates(self):
        a = {"status": "CONFIRMED", "total_jpy": 900, "included_ranges": [(1, 2)], "listing_url": "https://jp.mercari.com/item/a"}
        b = {"status": "CONFIRMED", "total_jpy": 900, "included_ranges": [(3, 4)], "listing_url": "https://jp.mercari.com/item/b"}
        c = {"status": "CONFIRMED", "total_jpy": 2200, "included_ranges": [(1, 4)], "listing_url": "https://jp.mercari.com/item/c"}
        result = minimum_cost_cover([a, b, c], 4)
        self.assertIsNotNone(result)
        self.assertEqual(result["total_jpy"], 1800)
        self.assertEqual(result["duplicate_volume_count"], 0)

    def test_price_is_parsed_only_from_structured_metadata(self):
        html = """
        <html><head><title>サンプル漫画セット</title></head><body>
        <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Product","name":"サンプル漫画 1-4巻セット",
         "offers":{"@type":"Offer","price":"3980","priceCurrency":"JPY",
         "availability":"https://schema.org/InStock","url":"https://jp.mercari.com/item/m123"}}
        </script>
        <p>参考価格 1,000円</p></body></html>
        """
        result = extract_product_data(html, "https://jp.mercari.com/item/m123")
        self.assertEqual(result["parse_status"], "OK")
        self.assertEqual(result["price_jpy"], 3980)
        self.assertEqual(result["availability"], "IN_STOCK")

    def test_meta_product_price_is_parsed_when_currency_is_explicit(self):
        html = """
        <html><head>
        <meta itemprop="price" content="1200">
        <meta itemprop="priceCurrency" content="JPY">
        <meta itemprop="availability" content="https://schema.org/InStock">
        </head><body>商品ページ</body></html>
        """
        result = extract_product_data(html, "https://jp.mercari.com/item/m123")
        self.assertEqual(result["parse_status"], "OK")
        self.assertEqual(result["price_jpy"], 1200)
        self.assertEqual(result["currency"], "JPY")

    def test_page_text_price_is_not_guessed(self):
        html = "<html><head><title>価格 1200円</title></head><body>中古 1200円</body></html>"
        result = extract_product_data(html, "https://jp.mercari.com/item/m123")
        self.assertEqual(result["parse_status"], "PRICE_NOT_FOUND")
        self.assertIsNone(result["price_jpy"])

    def test_aggregate_offer_low_price_is_not_checkout_price(self):
        html = """
        <script type="application/ld+json">
        {"@type":"Product","name":"全巻セット",
         "offers":{"@type":"AggregateOffer","lowPrice":"1200","highPrice":"2800",
         "priceCurrency":"JPY","availability":"https://schema.org/InStock"}}
        </script>
        """
        result = extract_product_data(html, "https://jp.mercari.com/item/m123")
        self.assertEqual(result["parse_status"], "PRICE_NOT_FOUND")
        self.assertIsNone(result["price_jpy"])

    def test_different_offer_prices_are_ambiguous(self):
        html = """
        <script type="application/ld+json">
        [{"@type":"Product","name":"セット","offers":[
          {"@type":"Offer","price":"1000","priceCurrency":"JPY","availability":"https://schema.org/InStock"},
          {"@type":"Offer","price":"2000","priceCurrency":"JPY","availability":"https://schema.org/InStock"}]}]
        </script>
        """
        result = extract_product_data(html, "https://jp.mercari.com/item/m123")
        self.assertEqual(result["parse_status"], "AMBIGUOUS_PRICE")
        self.assertIsNone(result["price_jpy"])


if __name__ == "__main__":
    unittest.main()
