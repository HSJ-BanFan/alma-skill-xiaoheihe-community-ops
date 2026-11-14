import base64
import io
import json
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "scripts"), str(Path(__file__).resolve().parent)]
import xhh
from browser import Browser
from documents import comment, document
from network_io import read_http, NetworkDeadline
from observations import difference, Store
from test_paging import PagedFixture
from urllib.request import Request
from xhh_sdk.config import XhhConfig
from xhh_sdk.transport import Transport


class FinalReviewFixTests(unittest.TestCase):
    def test_complete_network_worker_is_terminated_on_deadline(self):
        actual_run = subprocess.run
        def slow_worker(command, **kwargs):
            return actual_run([sys.executable, "-I", "-S", "-c", "import time;time.sleep(3)"], **kwargs)
        with patch("network_io.subprocess.run", side_effect=slow_worker), self.assertRaises(NetworkDeadline):
            read_http(Request("https://api.xiaoheihe.cn/bbs/app/link/tree"), .05)

    def test_reader_relays_bytes_and_never_places_cookie_in_arguments(self):
        captured = []
        def runner(command, **kwargs):
            captured.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "body": base64.b64encode(b"body").decode()}).encode())
        with patch("network_io.subprocess.run", side_effect=runner):
            value = read_http(Request("https://api.xiaoheihe.cn/read", headers={"Cookie": "fixture-secret"}), 1)
        self.assertEqual(value, b"body")
        self.assertNotIn("fixture-secret", " ".join(captured[0][0]))
        self.assertIn(b"fixture-secret", captured[0][1]["input"])

    def test_late_response_is_not_accepted_as_completed(self):
        clock = [0.0]
        config = XhhConfig(pkey="fixture", heybox_id="101", protocol_mode="web")
        transport = Transport(config)
        def delayed(request, timeout, **kwargs):
            clock[0] = 1.2
            return b'{"status":"ok","result":{"comments":[],"link":{"id":123},"total_page":1}}'
        with patch("paging.time.monotonic", side_effect=lambda: clock[0]), patch("paging.read_http", side_effect=delayed):
            data, scope = Browser(SimpleNamespace(config=config, transport=transport)).execute(xhh.parser().parse_args(["comments", "123", "--seconds", "1"]))
        self.assertEqual(scope["stop_reason"], "time_budget")
        self.assertNotEqual(scope["completion"], "complete")

    def test_empty_absolute_page_with_more_evidence_continues(self):
        class Fixture(PagedFixture):
            def signed_request(self, path, query=None):
                result = super().signed_request(path, query)
                if query["page"] == "1":
                    result["result"]["comments"] = []
                return result
        fixture = Fixture()
        data, scope = Browser(fixture).execute(xhh.parser().parse_args(["comments", "123", "--pages", "3"]))
        self.assertEqual([call[1]["page"] for call in fixture.calls], ["1", "2", "3"])
        self.assertEqual(scope["completion"], "complete")

    def test_raw_user_search_identity_deduplication_and_changes(self):
        class Fixture(PagedFixture):
            def signed_request(self, path, query=None):
                users = [1, 2] if query["offset"] == "0" else [2, 3]
                return {"result": {"items": [{"userid": uid, "username": "name"} for uid in users], "has_more": query["offset"] == "0"}}
        data, scope = Browser(Fixture()).execute(xhh.parser().parse_args(["search", "name", "--type", "user", "--pages", "2"]))
        self.assertEqual([item["uid"] for item in data["items"]],
                         ["1", "2", "3"])
        self.assertEqual(scope["duplicates_removed"], 1)
        self.assertEqual(difference({"items": [{"userid": 1, "username": "old"}]}, {"items": [{"userid": 1, "username": "new"}]})["changed"], ["1"])
        with tempfile.TemporaryDirectory() as temporary:
            Store(Path(temporary)).save("101", "people", "query", data, scope, [])

    def test_unknown_reply_target_is_not_assumed_to_be_root(self):
        class Fixture(PagedFixture):
            def signed_request(self, path, query=None):
                return {"result": {"comments": [{"comment": [{"commentid": 9, "userid": 202, "child_num": 1, "has_more": 0},
                                     {"commentid": 10, "userid": 404, "replyuserid": 303}]}], "total_page": 1}}
        data, scope = Browser(Fixture()).execute(xhh.parser().parse_args(["thread", "123", "9", "--around", "10"]))
        self.assertEqual(data["ancestor_ids"], ["10"])
        self.assertEqual(data["unknown_parent_ids"], ["10"])
        self.assertFalse(data["ancestry_complete"])
        self.assertIn("reply_target_unknown", scope["gaps"])

    def test_playable_media_aliases_and_missing_urls(self):
        for kind in ("video", "audio"):
            value = document({"id": 1, "is_full_text": 1, "text": [{"type": kind, "src": "https://cdn.max-c.com/media", "duration": 10}]}, {})
            self.assertEqual(value["media"][0]["url"], "https://cdn.max-c.com/media")
            self.assertEqual(value["media"][0]["data"]["duration"], 10)
            missing = document({"id": 1, "is_full_text": 1, "text": [{"type": kind, "src": "file:///missing"}]}, {})
            self.assertNotEqual(missing["body"]["completeness"], "complete")
            self.assertIn("playable_url_missing", missing["body"]["gaps"])

    def test_inline_comment_images_are_readable_and_affect_delta(self):
        before = comment({"commentid": 1, "text": 'before<img src="https://cdn.max-c.com/a.png">after'}, "123", "1")
        after = comment({"commentid": 1, "text": 'before<img src="https://cdn.max-c.com/b.png">after'}, "123", "1")
        self.assertNotEqual(before["text"], "beforeafter")
        self.assertEqual(before["media"][0]["url"], "https://cdn.max-c.com/a.png")
        self.assertEqual(difference({"items": [before]}, {"items": [after]})["changed"], ["1"])

    def test_heading_and_table_separators_are_retained(self):
        heading = document({"id": 1, "text": '<h1>Alpha</h1><h2>Beta</h2>'}, {})["body"]["text"]
        self.assertNotEqual(heading, "AlphaBeta")
        table = document({"id": 1, "text": '<table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>'}, {})["body"]["text"]
        self.assertIn("\t", table)
        self.assertIn("\n", table)

    def test_matching_media_metadata_enriches_body_reference(self):
        url = "https://cdn.max-c.com/a.png"
        post = document({"id": 1, "text": [{"type": "img", "url": url}],
                         "imgs": [{"url": url, "width": 64, "height": 32, "thumb_url": "https://cdn.max-c.com/thumb.png"}]}, {})
        self.assertEqual(len(post["media"]), 1)
        self.assertEqual((post["media"][0]["width"], post["media"][0]["height"]), (64, 32))
        self.assertEqual(post["media"][0]["preview_url"], "https://cdn.max-c.com/thumb.png")

    def test_comment_reading_does_not_require_post_body(self):
        class Fixture(PagedFixture):
            def signed_request(self, path, query=None):
                result = super().signed_request(path, query)
                result["result"].pop("link")
                return result
        data, scope = Browser(Fixture()).execute(xhh.parser().parse_args(["comments", "123"]))
        self.assertEqual(data["items"][0]["id"], "9")


if __name__ == "__main__":
    unittest.main()
