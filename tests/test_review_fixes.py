import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "scripts"), str(Path(__file__).resolve().parent)]
import xhh
from browser import Browser
from documents import document
from observations import Store
from test_paging import PagedFixture
from xhh_sdk.config import XhhConfig
from xhh_sdk.transport import Transport
from xhh_sdk.signer import Signer


class ReviewFixTests(unittest.TestCase):
    def test_snapshot_query_mismatches_return_structured_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "fixture.json"
            config.write_text(json.dumps({"pkey": "fixture", "heybox_id": "101", "protocol_mode": "web"}))
            store = Store(root)
            store.save("101", "first", "query-one", {"id": "1"}, {}, ["post", "1"])
            store.save("101", "second", "query-two", {"id": "2"}, {}, ["post", "2"])
            for arguments in (["feed", "7214", "--since", "first"], ["snapshot", "diff", "first", "second"]):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = xhh.main(["--config", str(config), "--data-dir", str(root), *arguments])
                self.assertEqual(code, 1)
                self.assertEqual(
                    json.loads(output.getvalue())["error"]["code"],
                    "configuration_error",
                )

    def test_snapshot_continuation_beyond_page_one_hundred(self):
        class Fixture(PagedFixture):
            def signed_request(self, path, query=None):
                result = super().signed_request(path, query)
                result["result"]["total_page"] = 102
                return result
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "fixture.json"
            config.write_text(json.dumps({"pkey": "fixture", "heybox_id": "101", "protocol_mode": "web"}))
            fixture = Fixture()
            def run(arguments):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = xhh.main(["--config", str(config), "--data-dir", str(root), *arguments], client_factory=lambda cfg: fixture)
                return code, json.loads(output.getvalue())
            self.assertEqual(run(["comments", "123", "--page", "100", "--snapshot", "long"])[0], 0)
            code, result = run(["snapshot", "resume", "long"])
            self.assertEqual(code, 0)
            self.assertEqual(fixture.calls[-1][1]["page"], "101")
            self.assertEqual(result["coverage"]["position"], 101)

    def test_sort_applies_to_combined_feed_scope(self):
        class Fixture(PagedFixture):
            def signed_request(self, path, query=None):
                times = [10, 20] if query["offset"] == "0" else [30, 40]
                return {"result": {"links": [{"linkid": value, "create_at": value, "comment_num": value,
                                              "link_award_num": value} for value in times], "has_more": query["offset"] == "0"}}
        for sorting in ("created", "comments", "charges"):
            data, scope = Browser(Fixture()).execute(xhh.parser().parse_args(["feed", "7214", "--pages", "2", "--sort", sorting]))
            self.assertEqual([item["id"] for item in data["items"]], ["40", "30", "20", "10"])
            self.assertEqual((scope["returned_total"], scope["selected"]), (4, 4))

    def test_overlapping_followers_can_be_saved(self):
        class Fixture(PagedFixture):
            def signed_request(self, path, query=None):
                users = [1, 2] if query["offset"] == "0" else [2, 3]
                return {"result": {"followers": [{"userid": uid} for uid in users], "has_more": query["offset"] == "0"}}
        data, scope = Browser(Fixture()).execute(xhh.parser().parse_args(["user", "202", "--view", "followers", "--pages", "2"]))
        self.assertEqual([item["uid"] for item in data["items"]], ["1", "2", "3"])
        self.assertEqual(scope["duplicates_removed"], 1)
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary))
            store.save("101", "people", "query", data, scope, [])
            self.assertEqual(len(store.get("101", "people")["data"]["items"]), 3)

    def test_media_outside_body_is_retained_without_full_body_claim(self):
        url = "https://imgheybox.max-c.com/p.png"
        post = document({"id": "1", "text": "", "imgs": [{"url": url, "width": 64}]}, {})
        self.assertEqual(post["media"][0]["url"], url)
        self.assertEqual(post["media"][0]["origin"], "link.imgs")
        self.assertEqual(post["body"]["status"], "missing")
        self.assertNotEqual(post["body"]["completeness"], "complete")
        same = document({"id": "1", "text": [{"type": "img", "url": url}], "imgs": [url]}, {})
        self.assertEqual(len(same["media"]), 1)

    def test_positive_root_continuation_overrides_low_count(self):
        class Fixture(PagedFixture):
            def signed_request(self, path, query=None):
                self.calls.append(path)
                if path.endswith("tree"):
                    return {"result": {"comments": [{"comment": [{"commentid": 9, "has_more": 1, "child_num": 0}]}], "total_page": 1}}
                return {"result": {"comments": [{"commentid": 10, "replyid": 9}], "has_more": False}}
        fixture = Fixture()
        data, scope = Browser(fixture).execute(xhh.parser().parse_args(["thread", "123", "9", "--around", "10", "--pages", "2"]))
        self.assertEqual(len(fixture.calls), 2)
        self.assertTrue(data["target_observed"])
        limited, limited_scope = Browser(Fixture()).execute(xhh.parser().parse_args(["thread", "123", "9"]))
        self.assertNotEqual(limited_scope["completion"], "complete")
        self.assertIsNotNone(limited_scope["continuation"])

    def test_app_signing_timeout_and_pre_dispatch_deadline(self):
        clock, network, signing_limits = [0.0], [], []
        class SlowSigner(Signer):
            def __init__(self):
                self.timeout = 60.0
            def sign(self, path):
                signing_limits.append(self.timeout)
                clock[0] = 2.0
                return {"_time": 1, "nonce": "fixture", "hkey": "fixture", "_rnd": "fixture"}
        config = XhhConfig(pkey="fixture", heybox_id="101", protocol_mode="app")
        signer = SlowSigner()
        transport = Transport(config, signer=signer)
        transport._open = lambda request: network.append(request) or b'{"status":"ok","result":{"link":{"id":123},"comments":[]}}'
        from types import SimpleNamespace
        with patch("paging.time.monotonic", side_effect=lambda: clock[0]):
            data, scope = Browser(SimpleNamespace(config=config, transport=transport)).execute(xhh.parser().parse_args(["comments", "123", "--seconds", "1"]))
        self.assertEqual(network, [])
        self.assertEqual(signing_limits, [1.0])
        self.assertEqual(scope["stop_reason"], "time_budget")
        self.assertEqual(signer.timeout, 60.0)
        self.assertEqual(config.timeout, 30.0)

    def test_raw_thread_spans_root_and_reply_requests(self):
        data, scope = Browser(PagedFixture()).execute(xhh.parser().parse_args(["thread", "123", "9", "--pages", "2", "--raw"]))
        self.assertEqual(len(data["raw_pages"]), 2)
        self.assertIn("comments", data["raw_pages"][1]["result"])


if __name__ == "__main__":
    unittest.main()
