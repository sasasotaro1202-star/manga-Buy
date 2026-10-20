import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from discover import SearchResultsParser, unwrap_search_url, is_product_candidate


class DiscoveryTests(unittest.TestCase):
    def test_parser_does_not_depend_on_one_css_class(self):
        html = """
        <html><body>
          <a href="https://jp.mercari.com/item/m123">Used manga set</a>
          <a class="result__a" href="https://shopping.bookoff.co.jp/used/123">BOOKOFF item</a>
        </body></html>
        """
        parser = SearchResultsParser()
        parser.feed(html)
        self.assertEqual(len(parser.items), 2)
        self.assertIn("Used manga set", parser.items[0]["title"])

    def test_duckduckgo_redirect_url_is_unwrapped(self):
        url = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fjp.mercari.com%2Fitem%2Fm123"
        self.assertEqual(unwrap_search_url(url), "https://jp.mercari.com/item/m123")

    def test_item_page_path_is_accepted_but_search_path_is_not(self):
        domains = ["jp.mercari.com", "mercari.com"]
        self.assertTrue(is_product_candidate("https://jp.mercari.com/item/m123", domains))
        self.assertFalse(is_product_candidate("https://jp.mercari.com/search?keyword=manga", domains))

    def test_http_url_is_rejected(self):
        self.assertFalse(is_product_candidate("http://jp.mercari.com/item/m123", ["mercari.com"]))


if __name__ == "__main__":
    unittest.main()
