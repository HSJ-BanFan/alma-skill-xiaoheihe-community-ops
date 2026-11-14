from __future__ import annotations

import concurrent.futures
import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(os.environ.get("XHH_TEST_SKILL_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "scripts"))

import xhh
from operations import Journal, OperationError


class Response:
    def __init__(self, value):
        self.body = json.dumps(value, ensure_ascii=False).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, *args):
        return self.body


class PlatformFixture:
    def __init__(self):
        self.calls = []
        self.missing_cid = False
        self.malformed_inbox = False
        self.fail_send = False
        self.identity = "101"
        self.status = "ok"

    def __call__(self, request, timeout=None):
        path = urlsplit(request.full_url).path
        if request.get_method() == "PUT":
            self.calls.append((path, "PUT", {"bytes": len(request.data)}, {}))
            return Response({})
        payload = parse_qs(request.data.decode()) if request.data else {}
        query = parse_qs(urlsplit(request.full_url).query)
        self.calls.append((path, request.get_method(), payload, query))
        if self.status != "ok":
            return Response({"status": self.status})
        if path == "/account/restore_login":
            result = {"userid": self.identity, "pkey": "fixture-secret-only"}
        elif path == "/bbs/app/link/tree":
            result = {"link": {"id": "501", "userid": "202", "text": "正文" * 500},
                      "comments": [{"comment": [{"commentid": "601", "userid": "202"},
                                                   {"commentid": "602", "userid": "303"}]}]}
        elif path == "/bbs/app/comment/sub/comments":
            result = {"comments": [{"commentid": "603", "userid": "404"}], "lastval": "next", "has_more": False}
        elif path == "/bbs/app/user/message":
            result = {} if self.malformed_inbox else {"messages": [{"comment_a_id": "602", "comment_b_id": "601",
                            "root_comment_id": "601", "linkid": "501", "comment_a_text": "完整来信" * 300,
                            "comment_b_text": "完整上文" * 300}]}
        elif path == "/bbs/app/comment/create":
            if self.fail_send:
                raise TimeoutError("fixture timeout after dispatch")
            return Response({"status": "ok", **({} if self.missing_cid else {"commentid": "701"})})
        elif path == "/bbs/app/api/link/post":
            result = {"link_id": "801"}
        elif path == "/bbs/app/link/like/combo":
            result = {"is_max_charge": False, "msg": "ok"}
        elif path.endswith("/upload/info/v2"):
            result = {"keys": ["/fixture.png"], "bucket": "fixture-12345", "region": "ap-shanghai"}
        elif path.endswith("/upload/token/v2"):
            result = {"credentials": {"tmpSecretId": "fixture-id", "tmpSecretKey": "fixture-key",
                                      "sessionToken": "fixture-token"}}
        elif path.endswith("/upload/callback/v2"):
            result = {"preview_urls": ["https://imgheybox.max-c.com/fixture.png"]}
        else:
            result = {"links": []}
        return Response({"status": "ok", "result": result})


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.config = self.folder / "fixture.json"
        self.config.write_text(json.dumps({"pkey": "fixture-secret-only", "heybox_id": "101", "protocol_mode": "web"}))
        self.text = self.folder / "body.txt"
        self.text.write_text("这是调用方确定的正文。", encoding="utf-8")
        self.fixture = PlatformFixture()

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *arguments):
        output = io.StringIO()
        with patch("urllib.request.urlopen", self.fixture), patch("paging.read_http", side_effect=lambda request, timeout, **kwargs: self.fixture(request, timeout).read()), contextlib.redirect_stdout(output):
            code = xhh.main(["--config", str(self.config), "--data-dir", str(self.folder / "data"), *arguments])
        return code, json.loads(output.getvalue())

    def comment(self, identity="comment-1", **kwargs):
        arguments = ["comment", "501", "--text-file", str(self.text), "--operation-id", identity, "--confirm"]
        if kwargs.get("url"):
            arguments.extend(["--image-url", kwargs["url"]])
        return self.run_cli(*arguments)

    def sends(self):
        return [call for call in self.fixture.calls if call[1] == "POST"]

    def test_missing_confirmation_precedes_config_and_network(self):
        code, value = self.run_cli("like", "501", "--operation-id", "like-1")
        self.assertEqual((code, value["error"]["code"], self.fixture.calls), (1, "confirmation_required", []))
        self.assertFalse((self.folder / "data").exists())

    def test_requires_explicit_account_without_creating_state(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = xhh.main(["inbox"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "account_selection_required")

    def test_public_comment_plain_text_has_no_gallery_or_host_dependency(self):
        code, value = self.comment()
        self.assertEqual(code, 0)
        self.assertEqual(value["data"]["receipt"]["comment_id"], "701")
        self.assertEqual(self.sends()[0][2]["text"], ["这是调用方确定的正文。"])
        self.assertEqual(self.sends()[0][2].get("imgs", []), [])
        self.assertNotIn("fixture-secret-only", json.dumps(value))
        self.assertEqual(value["coverage"]["public_visibility"], "not_checked")

    def test_replay_and_conflicting_content_never_resend(self):
        self.assertEqual(self.comment()[0], 0)
        code, value = self.comment()
        self.assertEqual(code, 0)
        self.assertTrue(value["data"]["replayed"])
        self.text.write_text("改过的正文", encoding="utf-8")
        code, value = self.comment()
        self.assertEqual(value["error"]["code"], "operation_id_conflict")
        self.assertEqual(len(self.sends()), 1)

    def test_uncertain_and_no_receipt_keep_reservation_closed(self):
        for missing in (False, True):
            with self.subTest(missing_cid=missing):
                self.fixture.fail_send = not missing
                self.fixture.missing_cid = missing
                identity = "uncertain-" + str(missing)
                code, value = self.comment(identity)
                self.assertEqual(value["error"]["code"], "write_uncertain")
                before = len(self.sends())
                self.fixture.fail_send = False
                self.fixture.missing_cid = False
                code, value = self.comment(identity)
                self.assertEqual(value["error"]["code"], "operation_pending_or_uncertain")
                self.assertEqual(len(self.sends()), before)

    def test_precise_reply_and_deep_cursor(self):
        base = ["reply", "501", "601", "602", "--text-file", str(self.text), "--operation-id", "reply-1", "--confirm"]
        self.assertEqual(self.run_cli(*base)[0], 0)
        payload = self.sends()[-1][2]
        self.assertEqual((payload["root_id"], payload["reply_id"]), (["601"], ["602"]))
        base[3] = "603"
        base[7] = "reply-2"
        code, value = self.run_cli(*base, "--cursor", "selected")
        self.assertEqual(code, 0)
        self.assertEqual(self.sends()[-1][2]["reply_id"], ["603"])

    def test_wrong_root_or_target_blocks_before_send(self):
        for root, target in (("999", "602"), ("601", "999")):
            code, value = self.run_cli("reply", "501", root, target, "--text-file", str(self.text),
                                       "--operation-id", "invalid-" + root + target, "--confirm")
            self.assertEqual(code, 1)
        self.assertEqual(self.sends(), [])

    def test_reply_image_flag_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            xhh.main(["reply", "501", "601", "602", "--text-file", str(self.text), "--operation-id", "reply",
                      "--confirm", "--image-url", "https://example.invalid/image.png"])
        self.assertEqual(error.exception.code, 2)

    def test_inbox_full_text_exact_relations_and_malformed_response(self):
        code, value = self.run_cli("inbox", "--offset", "20", "--limit", "5")
        self.assertEqual(code, 0)
        self.assertEqual(len(value["data"]["messages"][0]["comment_a_text"]), 1200)
        self.assertEqual(value["data"]["messages"][0]["comment_b_id"], "601")
        self.assertFalse(value["coverage"]["full_history"])
        self.assertEqual(value["coverage"]["next_offset"], 21)
        self.fixture.malformed_inbox = True
        self.assertEqual(self.run_cli("inbox")[1]["error"]["code"], "invalid_inbox_response")

    def test_identity_mismatch_and_captcha_stop_without_write(self):
        self.fixture.identity = "999"
        self.assertEqual(self.comment()[1]["error"]["code"], "account_identity_not_verified")
        self.fixture.identity = "101"
        self.fixture.status = "show_captcha"
        self.assertEqual(self.comment()[0], 1)
        self.assertEqual(self.sends(), [])

    def test_image_url_without_gallery_and_default_draft(self):
        self.assertEqual(self.comment(url="https://imgheybox.max-c.com/fixture.png")[0], 0)
        self.assertEqual(self.sends()[-1][2]["imgs"], ["https://imgheybox.max-c.com/fixture.png"])
        spec = self.folder / "post.json"
        spec.write_text(json.dumps({"content": "正文", "content_format": "text", "draft": False}), encoding="utf-8")
        code, value = self.run_cli("publish", str(spec), "--operation-id", "draft-1", "--confirm")
        self.assertEqual(code, 0)
        self.assertTrue(value["data"]["receipt"]["draft"])
        self.assertEqual(self.sends()[-1][2]["draft"], ["1"])
        code, value = self.run_cli("publish", str(spec), "--public", "--operation-id", "public-1", "--confirm")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.sends()), 2)

    def test_journal_serializes_concurrent_reservations(self):
        journal = Journal(self.folder / "concurrent")
        journal.read("101", "unused")
        count = []
        def run():
            try:
                return journal.execute("101", "same", "like", {"link_id": "501"},
                                       lambda: count.append(1) or {"ack": True})
            except OperationError as error:
                return error.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(lambda _: run(), range(6)))
        self.assertEqual(count, [1])
        self.assertEqual(journal.read("101", "same")["status"], "returned")

    def test_known_receipt_survives_persistence_failure(self):
        journal = Journal(self.folder / "failure")
        with patch.object(journal, "settle", side_effect=OSError("fixture disk full")):
            with self.assertRaises(OperationError) as error:
                journal.execute("101", "send-1", "comment", {}, lambda: {"comment_id": "701"})
        self.assertEqual(error.exception.receipt, {"comment_id": "701"})
        self.assertEqual(journal.read("101", "send-1")["status"], "reserved")

    def test_local_image_runs_sdk_cos_flow(self):
        image = self.folder / "pixel.png"
        image.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7xkAAAAASUVORK5CYII="))
        code, value = self.run_cli("upload", str(image), "--operation-id", "image-1", "--confirm")
        self.assertEqual(code, 0)
        receipt = value["data"]["receipt"]
        self.assertEqual((receipt["width"], receipt["height"]), (1, 1))
        self.assertEqual(receipt["url"], "https://imgheybox.max-c.com/fixture.png")
        self.assertEqual(len([call for call in self.fixture.calls if call[1] == "PUT"]), 1)
        self.assertEqual(len(self.sends()), 3)

    def test_public_post_and_delete_ownership(self):
        spec = self.folder / "public.json"
        spec.write_text(json.dumps({"content": "正文", "topic_ids": ["7214"], "original": False}), encoding="utf-8")
        code, value = self.run_cli("publish", str(spec), "--public", "--operation-id", "post-1", "--confirm")
        self.assertEqual(code, 0)
        self.assertFalse(value["data"]["receipt"]["draft"])
        self.assertEqual(self.sends()[-1][2]["draft"], ["0"])
        self.assertEqual(self.run_cli("delete-post", "501", "--operation-id", "delete-1", "--confirm")[0], 1)
        self.assertEqual(len(self.sends()), 1)

    def test_real_web_signer_and_untruncated_post(self):
        code, value = self.run_cli("post", "501")
        self.assertEqual(code, 0)
        self.assertEqual(len(value["data"]["body"]["text"]), 1000)
        query = self.fixture.calls[-1][3]
        self.assertIn("hkey", query)
        self.assertIn("_time", query)

    def test_uid_isolates_same_operation_id(self):
        journal = Journal(self.folder / "uids")
        calls = []
        for uid in ("101", "202"):
            journal.execute(uid, "same", "like", {"link_id": "501"}, lambda: calls.append(1) or {"ack": True})
        self.assertEqual(len(calls), 2)

    def test_website_reader_has_explicit_identity_and_required_os(self):
        code, value = self.run_cli("post", "501", "--reader", "website")
        self.assertEqual(code, 0)
        query = self.fixture.calls[-1][3]
        self.assertEqual(query["x_app"], ["heybox_website"])
        self.assertEqual(query["x_os_type"], ["Windows"])
        self.assertEqual(value["data"]["source"]["reader"], "website")
        self.assertEqual(len(self.fixture.calls), 1)

    def test_website_error_never_falls_back(self):
        self.fixture.status = "show_captcha"
        code, value = self.run_cli("post", "501", "--reader", "website")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.fixture.calls), 1)

    def test_website_reader_rejects_app_session_before_request(self):
        code, value = self.run_cli("--protocol", "app", "post", "501", "--reader", "website")
        self.assertEqual(code, 1)
        self.assertEqual(self.fixture.calls, [])


if __name__ == "__main__":
    unittest.main()
