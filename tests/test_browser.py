import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import xhh
from browser import Browser
from paging import coverage
from xhh_sdk.config import XhhConfig
from xhh_sdk.exceptions import XhhConfigError


class Fixture:
    def __init__(self):
        self.calls = []
        self.config = XhhConfig(pkey="fixture", heybox_id="101", protocol_mode="web")
        self.transport = self

    def signed_request(self, path, query=None):
        self.calls.append((path, query))
        if path.endswith("categories") or path.endswith("search"):
            return {"result": {"topics": [{"name": "category", "children": [{"topic_id": "7214", "name": "community"}]}]}}
        if path.endswith("list_infos"):
            return {"result": {"topic_infos": [{"topic_id": query["topic_ids"], "name": "community"}]}}
        if path.endswith("feeds"):
            return {"result": {"links": [{"linkid": "10", "is_article": 1, "create_at": 1, "comment_num": 5},
                                          {"linkid": "11", "link_tag": 27, "create_at": 2, "comment_num": 0}], "has_more": False}}
        if path.endswith("tree"):
            return {"result": {"link": {"id": "10", "text": "body"}, "comments": [], "total_page": 1}}
        raise AssertionError(path)


class BrowserTests(unittest.TestCase):
    def run_command(self, *argv):
        client = Fixture()
        data, scope = Browser(client).execute(xhh.parser().parse_args(argv))
        return data, scope, client.calls

    def test_discovery_preserves_category_path(self):
        data, scope, calls = self.run_command("community", "search", "name")
        self.assertEqual(data["items"][0]["category_path"], ["category"])
        self.assertEqual(data["items"][0]["id"], "7214")
        self.assertEqual(calls[0][1], {"q": "name"})

    def test_info_rejects_foreign_or_invalid_ids(self):
        with self.assertRaises(XhhConfigError):
            self.run_command("community", "info", "https://example.com")

    def test_feed_filters_keep_original_page_coverage(self):
        data, scope, calls = self.run_command("feed", "7214", "--content-type", "article")
        self.assertEqual([row["id"] for row in data["items"]], ["10"])
        self.assertEqual((scope["returned"], scope["selected"], scope["excluded"]), (2, 1, 1))
        self.assertEqual(scope["completion"], "complete")

    def test_local_sort_does_not_claim_global_order(self):
        data, scope, calls = self.run_command("feed", "7214", "--sort", "created")
        self.assertEqual(data["items"][0]["id"], "11")
        self.assertEqual(scope["sorting_scope"], "returned_items")

    def test_comments_only_and_sort_sent(self):
        data, scope, calls = self.run_command("comments", "10", "--sort", "time_desc")
        self.assertNotIn("body", data)
        self.assertEqual(calls[0][1]["sort_filter"], "time_desc")

    def test_no_marker_does_not_prove_end(self):
        self.assertEqual(coverage({}, count=0, position=0)["completion"], "unknown")
        self.assertEqual(coverage({"has_more": False}, count=0, position=0)["completion"], "complete")

    def test_conflicting_markers_preserve_uncertainty(self):
        result = coverage({"has_more": False, "total_page": 5}, count=20, position=1, mode="page")
        self.assertEqual(result["completion"], "unknown")
        self.assertIn("conflicting_page_markers", result["gaps"])


if __name__ == "__main__":
    unittest.main()
