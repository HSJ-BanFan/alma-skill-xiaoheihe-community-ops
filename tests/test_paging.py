import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import xhh
from browser import Browser
from xhh_sdk.config import XhhConfig
from xhh_sdk.exceptions import XhhAPIError, XhhConfigError, XhhTransportError


class PagedFixture:
    def __init__(self):
        self.config = XhhConfig(pkey="fixture", heybox_id="101", protocol_mode="web")
        self.transport = self
        self.calls = []
        self.fail_page = None

    def signed_request(self, path, query=None):
        self.calls.append((path, query))
        page = int(query.get("page", 1))
        if page == self.fail_page:
            raise XhhAPIError("stop", status="show_captcha")
        if path.endswith("tree"):
            root = {"commentid": 9, "text": "root", "child_num": 2, "has_more": 1}
            node = {"commentid": page + 9, "text": "reply", "replyid": 9}
            return {"result": {"link": {"id": 123, "text": "body"}, "comments": [{"comment": [root, node]}], "total_page": 3}}
        if path.endswith("sub/comments"):
            return {"result": {"comments": [{"commentid": 11, "replyid": 10, "text": "target"}], "has_more": False}}
        raise AssertionError(path)

    def account_info(self):
        return {"userid": self.config.heybox_id}


class PagingTests(unittest.TestCase):
    def test_budget_preserves_data_and_usable_resume(self):
        client = PagedFixture()
        browser = Browser(client)
        data, scope = browser.execute(xhh.parser().parse_args(["comments", "123", "--pages", "3", "--requests", "1"]))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(scope["stop_reason"], "request_budget")
        self.assertEqual(len(data["items"]), 2)
        data2, scope2 = browser.execute(xhh.parser().parse_args(["comments", "123", "--resume", scope["resume"]]))
        self.assertEqual(client.calls[-1][1]["page"], "2")
        self.assertEqual(data2["items"][1]["id"], "11")

    def test_resumed_target_and_account_must_match(self):
        client = PagedFixture()
        data, scope = Browser(client).execute(xhh.parser().parse_args(["comments", "123"]))
        with self.assertRaises(XhhConfigError):
            Browser(client).execute(xhh.parser().parse_args(["comments", "999", "--resume", scope["resume"]]))
        client.config.heybox_id = "202"
        with self.assertRaises(XhhConfigError):
            Browser(client).execute(xhh.parser().parse_args(["comments", "123", "--resume", scope["resume"]]))

    def test_paged_deduplication_and_proven_end(self):
        client = PagedFixture()
        data, scope = Browser(client).execute(xhh.parser().parse_args(["comments", "123", "--pages", "3"]))
        self.assertEqual([node["id"] for node in data["items"]], ["9", "10", "11", "12"])
        self.assertEqual(scope["duplicates_removed"], 2)
        self.assertEqual(scope["completion"], "complete")
        self.assertIsNone(scope["resume"])

    def test_error_preserves_read_data_and_stops(self):
        client = PagedFixture()
        client.fail_page = 2
        data, scope = Browser(client).execute(xhh.parser().parse_args(["comments", "123", "--pages", "3"]))
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(scope["stop_reason"], "platform_error")
        self.assertEqual(scope["error"]["status"], "show_captcha")
        self.assertEqual(scope["status"], "blocked")
        self.assertIsNone(scope["resume"])

    def test_retryable_error_after_data_keeps_safe_resume(self):
        class RetryableFixture(PagedFixture):
            def signed_request(self, path, query=None):
                if int(query.get("page", 1)) == 2:
                    self.calls.append((path, query))
                    raise XhhTransportError("temporary network failure")
                return super().signed_request(path, query)

        client = RetryableFixture()
        data, scope = Browser(client).execute(
            xhh.parser().parse_args([
                "comments", "123", "--pages", "3",
            ]))
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(scope["status"], "retryable_failure")
        self.assertIsNotNone(scope["resume"])

    def test_non_retryable_error_after_data_stops_without_resume(self):
        class BrokenShapeFixture(PagedFixture):
            def signed_request(self, path, query=None):
                if int(query.get("page", 1)) == 2:
                    self.calls.append((path, query))
                    raise XhhConfigError(
                        "unexpected comment payload",
                        code="platform_shape_invalid",
                    )
                return super().signed_request(path, query)

        client = BrokenShapeFixture()
        data, scope = Browser(client).execute(
            xhh.parser().parse_args([
                "comments", "123", "--pages", "3",
            ]))

        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(scope["status"], "blocked")
        self.assertEqual(scope["error"]["code"], "platform_shape_invalid")
        self.assertFalse(scope["error"]["retry_allowed"])
        self.assertIsNone(scope["resume"])

    def test_thread_resolves_ancestors_from_subpage(self):
        client = PagedFixture()
        data, scope = Browser(client).execute(xhh.parser().parse_args(["thread", "123", "9", "--around", "11", "--pages", "2"]))
        self.assertEqual(data["ancestor_ids"], ["11", "10", "9"])
        self.assertTrue(data["target_observed"])
        self.assertEqual(scope["completion"], "complete")

    def test_thread_sparse_duplicate_preserves_prior_parent_and_media(self):
        class SparseDuplicate(PagedFixture):
            def signed_request(self, path, query=None):
                if path.endswith("tree"):
                    root = {"commentid": 9, "text": "root", "child_num": 1, "has_more": 1}
                    reply = {"commentid": 10, "text": "target", "replyid": 9,
                             "imgs": [{"url": "https://cdn.max-c.com/target.png"}]}
                    return {"result": {"comments": [{"comment": [root, reply]}], "total_page": 1}}
                return {"result": {"comments": [{"commentid": 10, "text": "sparse duplicate"}], "has_more": False}}

        data, scope = Browser(SparseDuplicate()).execute(
            xhh.parser().parse_args(["thread", "123", "9", "--around", "10", "--pages", "2"]))
        target = next(item for item in data["items"] if item["id"] == "10")
        self.assertEqual(target["reply_to_id"], "9")
        self.assertEqual(target["media"][0]["url"], "https://cdn.max-c.com/target.png")
        self.assertEqual(data["ancestor_ids"], ["10", "9"])
        self.assertTrue(data["ancestry_complete"])
        self.assertEqual(scope["completion"], "complete")

    def test_thread_missing_duplicate_text_preserves_prior_evidence(self):
        class MissingText(PagedFixture):
            def signed_request(self, path, query=None):
                if path.endswith("tree"):
                    root = {"commentid": 9, "text": "root", "child_num": 1, "has_more": 1}
                    reply = {"commentid": 10, "text": "richer evidence", "replyid": 9}
                    return {"result": {"comments": [{"comment": [root, reply]}], "total_page": 1}}
                return {"result": {"comments": [{"commentid": 10}], "has_more": False}}

        data, scope = Browser(MissingText()).execute(
            xhh.parser().parse_args(["thread", "123", "9", "--around", "10", "--pages", "2"]))
        target = next(item for item in data["items"] if item["id"] == "10")
        self.assertEqual(target["text"], "richer evidence")
        self.assertEqual(scope["completion"], "complete")

    def test_unobserved_target_never_invented(self):
        client = PagedFixture()
        data, scope = Browser(client).execute(xhh.parser().parse_args(["thread", "123", "9", "--around", "999"]))
        self.assertFalse(data["target_observed"])
        self.assertIn("target_not_observed", scope["gaps"])


if __name__ == "__main__":
    unittest.main()
