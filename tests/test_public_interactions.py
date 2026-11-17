from __future__ import annotations

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
sys.path[:0] = [str(ROOT / "scripts"), str(Path(__file__).resolve().parent)]

import xhh
from messages import compose
from operations import Journal
from test_tools import PlatformFixture, Response
from xhh_sdk import cli as sdk_cli
from xhh_sdk.client import XhhClient
from xhh_sdk.config import XhhConfig
from xhh_sdk.exceptions import XhhAPIError, XhhConfigError


class PublicPlatform(PlatformFixture):
    def __init__(self):
        super().__init__()
        self.link_owner = "202"
        self.owned_comment_id = None

    def __call__(self, request, timeout=None):
        response = super().__call__(request, timeout)
        if urlsplit(request.full_url).path != "/bbs/app/link/tree" or self.status != "ok":
            return response
        value = json.loads(response.read())
        result = value["result"]
        if parse_qs(urlsplit(request.full_url).query).get("link_id") == ["999"]:
            result["link"] = None
        result["comments"].append({"comment": [{"commentid": "604", "userid": "404"},
                                                {"commentid": "605", "userid": "505"}]})
        for floor in result["comments"]:
            for node in floor["comment"]:
                if node["commentid"] == self.owned_comment_id:
                    node["userid"] = "101"
        return Response(value)


class PublicInteractionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.data = self.folder / "data"
        self.config = self.folder / "fixture.json"
        self.config.write_text(json.dumps({"pkey": "fixture-secret-only", "heybox_id": "101",
                                           "protocol_mode": "web"}), encoding="utf-8")
        self.text = self.folder / "body.txt"
        self.text.write_text("这是调用方确定的正文。", encoding="utf-8")
        self.platform = PublicPlatform()
        self.client = XhhClient(XhhConfig.from_file(self.config))

    def tearDown(self):
        self.temporary.cleanup()

    def cli(self, *arguments):
        output = io.StringIO()
        with patch("urllib.request.urlopen", self.platform), \
             patch("paging.read_http", side_effect=lambda request, timeout, **kwargs: self.platform(request, timeout).read()), \
             contextlib.redirect_stdout(output):
            code = xhh.main(["--config", str(self.config), "--data-dir", str(self.data), *arguments])
        return code, json.loads(output.getvalue())

    def send(self, kind, operation_id, *, composed=False, link_id="501", root_id="601",
             reply_id="602", confirm=True, extra=()):
        if composed:
            target = {"link_id": link_id}
            if kind == "reply":
                target.update(root_id=root_id, reply_id=reply_id)
            prepared = compose({"kind": kind, "target": target,
                                "content": [{"type": "text", "text": self.text.read_text(encoding="utf-8")}]}, self.data)
            arguments = ["send", "--prepared-json", json.dumps(prepared, ensure_ascii=False)]
        else:
            arguments = [kind, link_id]
            if kind == "reply":
                arguments.extend([root_id, reply_id])
            arguments.extend(["--text-file", str(self.text)])
        arguments.extend(["--operation-id", operation_id])
        if confirm:
            arguments.append("--confirm")
        return self.cli(*arguments, *extra)

    def sdk(self, kind, *arguments, **kwargs):
        with patch("urllib.request.urlopen", self.platform):
            return getattr(self.client, kind)(*arguments, **kwargs)

    def writes(self):
        return [call for call in self.platform.calls if call[1] == "POST"]

    def creates(self):
        return [call for call in self.writes() if call[0] == "/bbs/app/comment/create"]

    def test_foreign_reply_preserves_exact_target_and_selected_pagination(self):
        code, value = self.send("reply", "foreign-deep-reply", reply_id="603",
                                extra=("--page", "7", "--limit", "9", "--sort", "time_desc", "--cursor", "selected"))
        self.assertEqual(code, 0, value)
        self.assertEqual(len(self.creates()), 1)
        payload = self.creates()[0][2]
        self.assertEqual((payload["link_id"], payload["root_id"], payload["reply_id"]),
                         (["501"], ["601"], ["603"]))
        thread = [call for call in self.platform.calls if call[0] == "/bbs/app/link/tree" and "limit" in call[3]]
        self.assertEqual(len(thread), 1)
        self.assertEqual((thread[0][3]["page"], thread[0][3]["limit"], thread[0][3]["sort_filter"]),
                         (["7"], ["9"], ["time_desc"]))
        batch = [call for call in self.platform.calls if call[0] == "/bbs/app/comment/sub/comments"]
        self.assertEqual(len(batch), 1)
        self.assertEqual((batch[0][3]["root_comment_id"], batch[0][3]["lastval"]), (["601"], ["selected"]))
        receipt = value["data"]["receipt"]
        self.assertEqual((receipt["link_id"], receipt["root_id"], receipt["reply_id"]), ("501", "601", "603"))
        self.assertEqual(value["coverage"]["public_visibility"], "not_checked")

    def test_foreign_reply_rejects_wrong_root_floor_target_and_cursor(self):
        cases = [("999", "602", ()), ("602", "602", ()), ("601", "605", ()),
                 ("601", "999", ()), ("601", "602", ("--cursor", "selected"))]
        for index, (root, target, extra) in enumerate(cases):
            with self.subTest(root=root, target=target, extra=extra):
                operation_id = "invalid-foreign-reply-" + str(index)
                code, value = self.send("reply", operation_id, root_id=root, reply_id=target, extra=extra)
                self.assertEqual(code, 1, value)
                self.assertIsNone(Journal(self.data).read("101", operation_id))
        self.assertEqual(self.writes(), [])

    def test_raw_and_composed_foreign_sends_require_confirmation_before_requests(self):
        for composed in (False, True):
            for kind in ("comment", "reply"):
                with self.subTest(composed=composed, kind=kind):
                    code, value = self.send(kind, "unconfirmed", composed=composed, confirm=False)
                    self.assertEqual((code, value["error"]["code"]), (1, "confirmation_required"))
        self.assertEqual(self.platform.calls, [])
        self.assertFalse(self.data.exists())

    def test_account_mismatch_blocks_raw_and_composed_foreign_sends(self):
        self.platform.identity = "999"
        for composed in (False, True):
            for kind in ("comment", "reply"):
                with self.subTest(composed=composed, kind=kind):
                    operation_id = f"mismatch-{composed}-{kind}"
                    code, value = self.send(kind, operation_id, composed=composed)
                    self.assertEqual((code, value["error"]["code"]), (1, "account_identity_not_verified"))
                    self.assertIsNone(Journal(self.data).read("101", operation_id))
        self.assertEqual(self.writes(), [])

    def test_missing_post_blocks_raw_and_composed_sends_before_reservation(self):
        for composed in (False, True):
            for kind in ("comment", "reply"):
                with self.subTest(composed=composed, kind=kind):
                    operation_id = f"missing-{composed}-{kind}"
                    code, value = self.send(kind, operation_id, composed=composed, link_id="999")
                    self.assertEqual(code, 1, value)
                    self.assertIsNone(Journal(self.data).read("101", operation_id))
        self.assertEqual(self.writes(), [])

    def test_raw_and_composed_foreign_sends_replay_without_resending_or_changing_uid(self):
        for composed in (False, True):
            for kind in ("comment", "reply"):
                with self.subTest(composed=composed, kind=kind):
                    self.text.write_text("这是调用方确定的正文。", encoding="utf-8")
                    operation_id = f"replay-{composed}-{kind}"
                    before = len(self.creates())
                    code, value = self.send(kind, operation_id, composed=composed)
                    self.assertEqual(code, 0, value)
                    code, replay = self.send(kind, operation_id, composed=composed)
                    self.assertEqual(code, 0, replay)
                    self.assertTrue(replay["data"]["replayed"])
                    self.text.write_text("改过的正文", encoding="utf-8")
                    code, conflict = self.send(kind, operation_id, composed=composed)
                    self.assertEqual((code, conflict["error"]["code"]), (1, "operation_id_conflict"))
                    self.assertEqual(len(self.creates()), before + 1)
                    operation = Journal(self.data).read("101", operation_id)
                    self.assertEqual((operation["status"], operation["receipt"]["link_id"]), ("returned", "501"))
                    self.assertIsNone(Journal(self.data).read("202", operation_id))

    def test_uncertain_raw_and_composed_foreign_sends_stay_closed(self):
        for composed in (False, True):
            for kind in ("comment", "reply"):
                for missing in (False, True):
                    with self.subTest(composed=composed, kind=kind, missing_cid=missing):
                        operation_id = f"uncertain-{composed}-{kind}-{missing}"
                        self.platform.fail_send = not missing
                        self.platform.missing_cid = missing
                        code, value = self.send(kind, operation_id, composed=composed)
                        self.assertEqual((code, value["error"]["code"]), (1, "write_uncertain"))
                        self.assertEqual(Journal(self.data).read("101", operation_id)["status"], "uncertain")
                        before = len(self.creates())
                        self.platform.fail_send = self.platform.missing_cid = False
                        code, retry = self.send(kind, operation_id, composed=composed)
                        self.assertEqual((code, retry["error"]["code"]), (1, "operation_pending_or_uncertain"))
                        self.assertEqual(len(self.creates()), before)

    def test_direct_sdk_comment_and_reply_accept_another_accounts_post(self):
        created = self.sdk("comment", "501", "fixture comment", confirm=True)
        self.assertEqual((created["link_id"], created["comment_id"]), ("501", "701"))
        for target in ("601", "602"):
            with self.subTest(target=target):
                replied = self.sdk("reply", "501", "601", target, "fixture reply", confirm=True)
                self.assertEqual((replied["link_id"], replied["root_id"], replied["reply_id"]), ("501", "601", target))
                self.assertEqual((self.creates()[-1][2]["root_id"], self.creates()[-1][2]["reply_id"]), (["601"], [target]))
        self.assertEqual(len(self.creates()), 3)
        self.assertTrue(all(call[2]["link_id"] == ["501"] for call in self.creates()))

    def test_direct_sdk_participation_requires_confirmation_before_reads(self):
        for kind, arguments in (("comment", ("501", "fixture")),
                                ("reply", ("501", "601", "602", "fixture"))):
            with self.subTest(kind=kind), self.assertRaises(XhhConfigError):
                self.sdk(kind, *arguments)
        self.assertEqual(self.platform.calls, [])

    def test_direct_sdk_participation_rejects_nonexistent_posts(self):
        for kind, arguments in (("comment", ("999", "fixture")),
                                ("reply", ("999", "601", "602", "fixture"))):
            with self.subTest(kind=kind), self.assertRaises(XhhAPIError):
                self.sdk(kind, *arguments, confirm=True)
        self.assertEqual(self.writes(), [])

    def test_direct_sdk_reply_retains_exact_first_page_root_and_target_checks(self):
        for root, target in (("999", "602"), ("602", "602"), ("601", "605"), ("601", "603")):
            with self.subTest(root=root, target=target), self.assertRaises(XhhConfigError):
                self.sdk("reply", "501", root, target, "fixture", confirm=True)
        self.assertEqual(self.writes(), [])

    def test_foreign_post_edit_delete_and_foreign_comment_delete_remain_blocked(self):
        spec = self.folder / "edit.json"
        spec.write_text(json.dumps({"content": "fixture edit", "content_format": "text", "edit_link_id": "501"}), encoding="utf-8")
        cases = [("foreign-edit", ["publish", str(spec)]),
                 ("foreign-delete", ["delete-post", "501"]),
                 ("foreign-comment-delete", ["delete-comment", "501", "602"])]
        for operation_id, arguments in cases:
            with self.subTest(operation_id=operation_id):
                self.platform.calls.clear()
                code, value = self.cli(*arguments, "--operation-id", operation_id, "--confirm")
                self.assertEqual(code, 1, value)
                self.assertTrue(any(call[0] == "/bbs/app/link/tree" for call in self.platform.calls))
                self.assertEqual(self.writes(), [])
                self.assertIsNone(Journal(self.data).read("101", operation_id))
        with self.assertRaises(XhhConfigError):
            self.sdk("delete_comment", "501", "602", confirm=True)
        self.assertEqual(self.writes(), [])

    def test_own_comment_can_still_be_deleted_from_another_accounts_post(self):
        self.platform.owned_comment_id = "602"
        deleted = self.sdk("delete_comment", "501", "602", confirm=True)
        self.assertEqual(deleted, {"comment_id": "602", "link_id": "501", "deleted": True})
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.writes()[0][:3], ("/bbs/app/comment/delete", "POST", {"comment_id": ["602"]}))

    def test_sdk_canary_keeps_its_own_post_scope_before_commenting(self):
        with patch("urllib.request.urlopen", self.platform), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = sdk_cli.main(["--config", str(self.config), "canary", "501", "--wait", "0", "--confirm"])
        self.assertEqual(code, 1)
        self.assertTrue(any(call[0] == "/bbs/app/link/tree" for call in self.platform.calls))
        self.assertEqual(self.writes(), [])


if __name__ == "__main__":
    unittest.main()
