import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "scripts"), str(Path(__file__).resolve().parent)]
import xhh
import network_io
from browser import Browser
from test_paging import PagedFixture
from xhh_sdk.exceptions import XhhConfigError


class LatestReviewFixTests(unittest.TestCase):
    def test_worker_preserves_idle_timeout_separately_from_deadline(self):
        observed = []
        payload = {"url": "https://api.xiaoheihe.cn/read", "headers": {}, "timeout": 1, "http_timeout": .1, "profile": "api"}
        def reader(transport, request):
            observed.append(transport.config.timeout)
            return b"response"
        output = io.StringIO()
        with patch("network_io.sys.stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))), patch("network_io.sys.stdout", output), patch("xhh_sdk.transport.Transport._open", reader):
            network_io.worker()
        self.assertEqual(observed, [.1])
        self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_later_replies_enrich_duplicate_relationship_and_media(self):
        class Fixture(PagedFixture):
            def signed_request(self, path, query=None):
                node = {"commentid": 10, "text": "reply"}
                if query["page"] == "2":
                    node.update(replyid=9, imgs=["https://cdn.max-c.com/a.png"])
                return {"result": {"comments": [{"comment": [{"commentid": 9, "text": "root"}, node]}], "total_page": 2}}
        data, scope = Browser(Fixture()).execute(xhh.parser().parse_args(["comments", "123", "--pages", "2"]))
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["items"][1]["reply_to_id"], "9")
        self.assertEqual(data["items"][1]["media"][0]["url"], "https://cdn.max-c.com/a.png")

    def test_missing_next_cursor_never_reuses_consumed_cursor(self):
        class Fixture(PagedFixture):
            def signed_request(self, path, query=None):
                if path.endswith("tree"):
                    return {"result": {"comments": [{"comment": [{"commentid": 9, "child_num": 5, "has_more": 1}]}], "total_page": 1}}
                return {"result": {"comments": [{"commentid": 10, "replyid": 9}], "has_more": True,
                                   **({"lastval": "c1"} if "lastval" not in query else {})}}
        data, scope = Browser(Fixture()).execute(xhh.parser().parse_args(["thread", "123", "9", "--pages", "3"]))
        self.assertIsNone(scope["continuation"])
        self.assertIsNone(scope["resume"])
        self.assertIn("continuation_missing", scope["gaps"])

    def test_non_object_comment_page_preserves_previous_data(self):
        class Fixture(PagedFixture):
            def signed_request(self, path, query=None):
                value = super().signed_request(path, query)
                if query["page"] == "2":
                    value["result"]["comments"] = [{"comment": [None]}]
                return value
        data, scope = Browser(Fixture()).execute(xhh.parser().parse_args(["comments", "123", "--pages", "2"]))
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(scope["stop_reason"], "platform_error")
        self.assertEqual(scope["error"]["type"], "XhhConfigError")

    def test_posts_view_rejects_returned_wrong_author(self):
        class Fixture(PagedFixture):
            def signed_request(self, path, query=None):
                return {"post_links": [{"linkid": 1}], "user": {"userid": 999}}
        with self.assertRaises(XhhConfigError):
            Browser(Fixture()).execute(xhh.parser().parse_args(["user", "202", "--view", "posts"]))


if __name__ == "__main__":
    unittest.main()
