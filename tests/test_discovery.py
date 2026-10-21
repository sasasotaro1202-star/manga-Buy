import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from discover import SearchResultsParser, MarketplaceResultsParser, unwrap_search_url, is_product_candidate, parse_bing_rss, search_engine_urls, search_queries_for_manga


class DiscoveryTests(unittest.TestCase):
    def test_parser_does_not_depend_on_one_css_class(self):
        html = """
        <html><body>
          <a href="https://www.bing.com/?FORM=Z9FD1">Bing home</a>
          <a href="https://www.bing.com/search?q=manga">Search again</a>
          <li class="b_algo"><h2>
            <a href="https://jp.mercari.com/item/m123">Used manga set</a>
          </h2></li>
          <a class="result__a" href="https://shopping.bookoff.co.jp/used/123">BOOKOFF item</a>
          <div class="related-search"><a href="https://hunterboots.com/">Related topic</a></div>
        </body></html>
        """
        parser = SearchResultsParser()
        parser.feed(html)
        self.assertEqual(len(parser.items), 2)
        self.assertEqual(parser.items[0]["title"], "Used manga set")
        self.assertTrue(any("BOOKOFF item" in item["title"] for item in parser.items))

    def test_duckduckgo_redirect_url_is_unwrapped(self):
        url = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fjp.mercari.com%2Fitem%2Fm123"
        self.assertEqual(unwrap_search_url(url), "https://jp.mercari.com/item/m123")

    def test_item_page_path_is_accepted_but_search_path_is_not(self):
        domains = ["jp.mercari.com", "mercari.com"]
        self.assertTrue(is_product_candidate("https://jp.mercari.com/item/m123", domains))
        self.assertFalse(is_product_candidate("https://jp.mercari.com/search?keyword=manga", domains))

    def test_bing_rss_parser_extracts_product_links_without_prices(self):
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel><item>
          <title>Used manga set</title><link>https://jp.mercari.com/item/m123</link>
          <description>4000 yen</description>
        </item></channel></rss>"""
        results = parse_bing_rss(xml)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Used manga set")
        self.assertEqual(results[0]["url"], "https://jp.mercari.com/item/m123")
        self.assertNotIn("price_jpy", results[0])

    def test_bing_rss_parser_rejects_malformed_xml(self):
        with self.assertRaises(Exception):
            parse_bing_rss(b"<rss>")

    def test_relative_bing_result_uses_bing_as_base(self):
        # Search pages often return an absolute-path redirect rather than a full URL.
        relative = "/ck/a?u=a1aHR0cHM6Ly9qcC5tZXJjYXJpLmNvbS9pdGVtL20xMjM="
        base = "https://www.bing.com/search?q=manga"
        self.assertEqual(unwrap_search_url(relative, base), "https://jp.mercari.com/item/m123")

    def test_bing_redirect_url_is_unwrapped(self):
        # Bing encodes the destination URL as base64url after the "a1" prefix.
        url = "https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9qcC5tZXJjYXJpLmNvbS9pdGVtL20xMjM="
        self.assertEqual(unwrap_search_url(url), "https://jp.mercari.com/item/m123")

    def test_default_search_fallback_includes_bing_html(self):
        urls = search_engine_urls({"engine_url": "https://html.duckduckgo.com/html/?q={query}"})
        self.assertEqual(len(urls), 3)
        self.assertIn("duckduckgo.com", urls[0])
        self.assertIn("bing.com/search?q=", urls[1])
        self.assertIn("format=rss", urls[2])
        self.assertTrue(all(url.startswith("https://") and "{query}" in url for url in urls))

    def test_queries_target_marketplaces_individually(self):
        config = {
            "search_targets": [
                {"id": "mercari", "domain": "jp.mercari.com",
                 "query_template": 'site:jp.mercari.com/item "{title}" 中古 全巻セット'},
                {"id": "bookoff", "domain": "shopping.bookoff.co.jp",
                 "query_template": 'site:shopping.bookoff.co.jp/used "{title}" 全巻セット'},
            ]
        }
        queries = search_queries_for_manga({"search_title": "HUNTER×HUNTER"}, config)
        self.assertEqual([query["id"] for query in queries], ["mercari", "bookoff"])
        self.assertIn("site:jp.mercari.com/item", queries[0]["query"])
        self.assertIn("site:shopping.bookoff.co.jp/used", queries[1]["query"])
        self.assertTrue(all(" OR " not in query["query"] for query in queries))
        self.assertTrue(all("HUNTER×HUNTER" in query["query"] for query in queries))

    def test_marketplace_parser_extracts_item_links_from_direct_search_page(self):
        html = """
        <html><body>
          <a href="/search?keyword=manga">Search page</a>
          <a href="/item/m123"><span>Used manga full set</span></a>
          <a href="https://jp.mercari.com/item/m456">Another listing</a>
        </body></html>
        """
        parser = MarketplaceResultsParser()
        parser.feed(html)
        self.assertEqual(len(parser.items), 3)
        self.assertEqual(parser.items[1]["url"], "/item/m123")
        self.assertIn("Used manga full set", parser.items[1]["title"])

    def test_seed_file_contains_only_urls_and_unknown_observation_values(self):
        import json
        seed_path = Path(__file__).resolve().parents[1] / "config" / "manual_candidate_seeds.json"
        data = json.loads(seed_path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["candidates"]), 25)
        self.assertEqual(len({row["manga_id"] for row in data["candidates"]}), 25)
        for row in data["candidates"]:
            with self.subTest(manga_id=row["manga_id"]):
                self.assertTrue(row["listing_url"].startswith("https://jp.mercari.com/"))
                self.assertEqual(row["verification_status"], "UNVERIFIED_SEED")
                self.assertIsNone(row["price_jpy"])
                self.assertEqual(row["availability"], "UNKNOWN")
                self.assertEqual(row["condition"], "UNKNOWN")
                self.assertIsNone(row["shipping_jpy"])

    def test_http_url_is_rejected(self):
        self.assertFalse(is_product_candidate("http://jp.mercari.com/item/m123", ["mercari.com"]))


if __name__ == "__main__":
    unittest.main()
