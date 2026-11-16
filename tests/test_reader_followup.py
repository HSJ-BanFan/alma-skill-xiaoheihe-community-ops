import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import xhh
from observations import Store
from paging import decode_resume
from xhh_sdk.client import XhhClient
from xhh_sdk.config import XhhConfig


class CommandClient:
    def __init__(self, config, handler):
        self.config = config
        self.transport = self
        self.handler = handler
        self.calls = []

    def signed_request(self, path, query=None):
        self.calls.append((path, query))
        return copy.deepcopy(self.handler(path, query))

    def account_info(self):
        return {"userid": self.config.heybox_id}


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = XhhConfig(pkey="synthetic", heybox_id="101")

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, arguments, handler, *, public=False):
        created = []

        def factory(config):
            client = CommandClient(config, handler)
            created.append(client)
            return client

        output = io.StringIO()
        with patch("xhh.load_config", return_value=self.config) if not public else contextlib.nullcontext(), \
                patch("xhh_sdk.transport.Transport._open", side_effect=AssertionError("unmocked network request")), \
                contextlib.redirect_stdout(output):
            code = xhh.main(["--data-dir", str(self.root), *arguments],
                            client_factory=XhhClient if public else factory)
        return code, json.loads(output.getvalue()), created


class ReaderFollowupTests(CommandTests):
    def test_public_html_snapshot_and_since_use_one_public_get(self):
        requests = []

        def public_get(request, *args, **kwargs):
            requests.append(request)
            self.assertEqual(kwargs["profile"], "public")
            return b"<article><p>public body</p></article>"

        with patch("browser.read_http", side_effect=public_get), \
                patch("operations.verify_identity", side_effect=AssertionError("identity request")), \
                patch("xhh.AccountStore", side_effect=AssertionError("account access")), \
                patch("xhh_sdk.transport.Signer", side_effect=AssertionError("signer access")):
            for arguments in (
                ["post", "123", "--reader", "html", "--requests", "1", "--snapshot", "public"],
                ["post", "123", "--reader", "html", "--requests", "1", "--since", "public"],
            ):
                code, value, _ = self.run_cli(arguments, None, public=True)
                self.assertEqual(code, 0, value)
                self.assertTrue(value["ok"])
                self.assertEqual(value["coverage"]["budget"]["used_requests"], 1)
                self.assertFalse(value["data"]["source"]["credentials_sent"])
                self.assertFalse(value["data"]["source"]["identity_verified"])
            self.assertEqual(value["coverage"]["delta"]["unchanged"], ["123"])
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.full_url, "https://www.xiaoheihe.cn/app/bbs/link/123")
            self.assertIsNone(request.get_header("Cookie"))
            self.assertIsNone(request.get_header("Authorization"))

    def test_post_raw_survives_cli_but_does_not_change_snapshot(self):
        for reader, public in (("native", False), ("html", True)):
            with self.subTest(reader=reader):
                name = "raw-" + reader
                payload = {"status": "ok", "result": {"link": {
                    "id": "123", "text": "body", "is_full_text": 1,
                }}, "transport_nonce": "first"}
                html = b"<article>body</article><!-- first transport -->"
                with patch("browser.read_http", side_effect=lambda *args, **kwargs: html):
                    code, first, _ = self.run_cli(
                        ["post", "123", "--reader", reader, "--raw", "--snapshot", name],
                        lambda *args: payload, public=public)
                    self.assertEqual(code, 0, first)
                    self.assertTrue(first["data"]["raw_responses"])
                    payload["transport_nonce"] = "second"
                    html = b"<article>body</article><!-- second transport -->"
                    code, second, _ = self.run_cli(
                        ["post", "123", "--reader", reader, "--raw", "--since", name],
                        lambda *args: payload, public=public)
                    self.assertEqual(code, 0, second)
                    self.assertNotEqual(first["data"]["raw_responses"], second["data"]["raw_responses"])
                    self.assertEqual(second["coverage"]["delta"]["changed"], [])
                    self.assertEqual(second["coverage"]["delta"]["unchanged"], ["123"])
                    code, clean, _ = self.run_cli(
                        ["post", "123", "--reader", reader, "--since", name],
                        lambda *args: payload, public=public)
                    self.assertEqual(code, 0, clean)
                    self.assertNotIn("raw_responses", clean["data"])
                    self.assertEqual(clean["coverage"]["delta"]["unchanged"], ["123"])

    def test_legacy_budget_diagnostic_replays_partial_with_resume(self):
        for reason in ("page_budget", "request_budget", "time_budget", "item_budget"):
            for has_reason in (True, False):
                with self.subTest(reason=reason, has_reason=has_reason):
                    scope = {"completion": "more", "resume": "safe-position", "error": {
                        "code": reason + "_exhausted", "retry_allowed": False,
                    }}
                    if has_reason:
                        scope["stop_reason"] = reason
                    name = reason + str(has_reason)
                    Store(self.root).save("101", name, "query", {"items": [{"id": "1"}]},
                                          scope, ["comments", "123"], pending_response={
                                              "ok": False, "data": {"items": [{"id": "1"}]},
                                              "coverage": scope, "error": scope["error"],
                                          })
                    code, value, _ = self.run_cli(["snapshot", "resume", name],
                                                  lambda *args: self.fail("offline replay requested network"))
                    self.assertEqual(code, 0, value)
                    self.assertTrue(value["ok"])
                    self.assertEqual(value["coverage"]["status"], "partial")
                    self.assertEqual(value["coverage"]["resume"], "safe-position")
                    self.assertIsNone(value["error"])

    def test_platform_blocks_override_legacy_budget_reason(self):
        for index, (error, expected) in enumerate((
            ({"code": "captcha_required", "retry_allowed": False}, "captcha_required"),
            ({"status": "show_captcha", "retry_allowed": True}, "captcha_required"),
            ({"code": "rate_limited", "retry_allowed": False}, "rate_limited"),
            ({"status": "HTTP_429", "retry_allowed": True}, "rate_limited"),
        )):
            with self.subTest(error=error):
                scope = {"status": "partial", "stop_reason": "page_budget",
                         "resume": "unsafe", "error": error}
                name = "blocked" + str(index)
                Store(self.root).save("101", name, "query", {"items": [{"id": "1"}]},
                                      scope, [], pending_response={"ok": True, "data": {},
                                                                   "coverage": scope, "error": error})
                code, value, _ = self.run_cli(["snapshot", "resume", name], None)
                self.assertEqual(code, 1, value)
                self.assertEqual(value["coverage"]["status"], "blocked")
                self.assertIsNone(value["coverage"]["resume"])
                self.assertEqual(value["error"]["code"], expected)

    def test_thread_public_continuation_replays_actual_mid_page(self):
        def handler(path, query):
            page = int(query["page"])
            records = [] if page == 1 else [
                {"commentid": "9", "text": "root", "child_num": 3, "has_more": 0},
                *[{"commentid": str(index), "text": "reply", "replyid": "9"}
                  for index in range(10, 13)],
            ]
            return {"result": {"comments": [{"comment": records}] if records else [], "total_page": 2}}

        arguments = ["thread", "123", "9", "--pages", "2", "--limit", "2"]
        code, first, clients = self.run_cli(arguments, handler)
        self.assertEqual(code, 0, first)
        self.assertEqual([item["id"] for item in first["data"]["items"]], ["9", "10"])
        token = decode_resume(first["coverage"]["resume"], "thread", "101")
        self.assertEqual(token["page"], 2)
        self.assertEqual(token["item_window"]["position"], 2)
        self.assertEqual(first["coverage"]["continuation"]["page"], 2)
        self.assertEqual([query["page"] for _, query in clients[0].calls], ["1", "2"])
        code, second, clients = self.run_cli(arguments + ["--resume", first["coverage"]["resume"]], handler)
        self.assertEqual(code, 0, second)
        self.assertEqual([query["page"] for _, query in clients[0].calls], ["2"])
        self.assertEqual([item["id"] for item in second["data"]["items"]], ["9", "11"])


if __name__ == "__main__":
    unittest.main()
