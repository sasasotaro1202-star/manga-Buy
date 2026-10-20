import unittest

from manga_core import ROOT, load_json, manga_map, expected_count


class CatalogueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_json(ROOT / "config" / "manga_catalog.json", {})
        cls.entries = cls.catalog.get("manga", [])

    def test_all_user_selected_works_are_present(self):
        self.assertEqual(len(self.entries), 25)
        self.assertEqual(sum(item.get("candidate_group") == "first" for item in self.entries), 9)
        self.assertEqual(sum(item.get("candidate_group") == "second" for item in self.entries), 16)

    def test_ids_are_unique_and_volume_maps_are_consistent(self):
        ids = [item.get("id") for item in self.entries]
        self.assertEqual(len(ids), len(set(ids)))
        for item in self.entries:
            groups = item.get("volume_groups", [])
            self.assertGreater(len(groups), 0)
            for group in groups:
                self.assertEqual(group["seq_end"] - group["seq_start"] + 1, group["count"])

    def test_hunter_official_volume_count_source_is_present(self):
        item = manga_map(self.catalog)["hunter-x-hunter"]
        count, confirmed = expected_count(item)
        self.assertEqual(count, 39)
        self.assertTrue(confirmed)
        self.assertTrue(item["volume_count_source_url"].startswith("https://"))

    def test_ongoing_unverified_count_does_not_become_confirmed(self):
        item = manga_map(self.catalog)["frieren"]
        count, confirmed = expected_count(item)
        self.assertEqual(count, 15)
        self.assertFalse(confirmed)


if __name__ == "__main__":
    unittest.main()
