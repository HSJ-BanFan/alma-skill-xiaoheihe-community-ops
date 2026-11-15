from __future__ import annotations

import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(os.environ.get("XHH_TEST_SKILL_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT / "scripts"), str(Path(__file__).resolve().parent)]

from interaction_history import InteractionHistory, Scope
from test_interaction_cli import InteractionFixture
import test_tools
from test_tools import Response


class ReviewFixture(InteractionFixture):
    def __init__(self):
        super().__init__()
        self.public_own = True
        self.public_text = 'inline <img src="https://imgheybox.max-c.com/inline.png"> image'

    def __call__(self, request, timeout=None):
        value = json.loads(super().__call__(request, timeout).read())
        path = urlsplit(request.full_url).path
        if path == "/bbs/app/profile/bbs/comment/list" and isinstance(value.get("result"), list):
            for row in value["result"]:
                row.pop("userid", None)
                row["comment_images"] = ["https://imgheybox.max-c.com/own-list.png"]
        if path == "/bbs/app/link/tree" and isinstance(value.get("result"), dict):
            for floor in value["result"].get("comments", []):
                for node in floor["comment"]:
                    if node["commentid"] == "701":
                        node["text"] = self.public_text
        return Response(value)


class InteractionReviewRegressionTest(unittest.TestCase):
    setUp = test_tools.ToolsTest.setUp
    tearDown = test_tools.ToolsTest.tearDown
    run_cli = test_tools.ToolsTest.run_cli

    def store(self):
        return InteractionHistory(self.folder / "data")

    def own(self):
        return next(row for row in self.store().history(Scope("101", "501"))["items"] if row["id"] == "701")

    def test_actual_own_comment_images_remain_in_live_response_only(self):
        self.fixture = ReviewFixture()
        code, value = self.run_cli("my-comments")
        self.assertEqual(code, 0)
        self.assertTrue(value["ok"])
        self.assertEqual(value["data"][0]["comment_images"], ["https://imgheybox.max-c.com/own-list.png"])
        self.assertFalse({"text", "raw_text", "media", "raw"} & self.own().keys())

    def test_explicit_text_edit_removes_old_inline_image(self):
        self.fixture = ReviewFixture()
        self.assertEqual(self.run_cli("account-info")[0], 0)
        code, first = self.run_cli("comments", "501", "--requests", "1")
        self.assertEqual(code, 0)
        own = next(row for row in first["data"]["items"] if row["id"] == "701")
        self.assertIn("https://imgheybox.max-c.com/inline.png", [asset["url"] for asset in own["media"]])
        self.fixture.public_text = "image removed"
        code, edited = self.run_cli("comments", "501", "--requests", "1")
        self.assertEqual(code, 0)
        own = next(row for row in edited["data"]["items"] if row["id"] == "701")
        self.assertEqual(own["text"], "image removed")
        self.assertEqual(own["media"], [])
        self.assertNotIn("media", self.own())

    def test_own_list_attachment_and_later_public_body_stay_live_only(self):
        self.fixture = ReviewFixture()
        code, own_list = self.run_cli("my-comments")
        self.assertEqual(code, 0)
        self.assertEqual(own_list["data"][0]["comment_images"], ["https://imgheybox.max-c.com/own-list.png"])
        self.assertEqual(self.run_cli("comments", "501", "--requests", "1")[0], 0)
        self.fixture.public_text = "inline image removed, attachment not returned"
        code, current = self.run_cli("comments", "501", "--requests", "1")
        self.assertEqual(code, 0)
        own = next(row for row in current["data"]["items"] if row["id"] == "701")
        self.assertEqual(own["text"], self.fixture.public_text)
        self.assertEqual(own["media"], [])
        self.assertEqual(self.own()["author"]["uid"], "101")
        self.assertNotIn("media", self.own())

    def test_sync_identity_persistence_failure_keeps_prior_status_and_resume(self):
        self.fixture = ReviewFixture()
        code, first = self.run_cli("interaction", "sync", "501", "--requests", "3", "--pages", "2")
        self.assertEqual(code, 0)
        token = first["coverage"]["resume"]
        self.assertTrue(token)
        with patch.object(InteractionHistory, "establish", side_effect=OSError("fixture identity storage failure")):
            code, failed = self.run_cli("interaction", "sync", "501", "--resume", token)
        self.assertEqual(code, 1)
        self.assertEqual(failed["coverage"].get("stop_reason"), "storage_error")
        self.assertEqual(failed["coverage"]["resume"], token)
        self.assertEqual(failed["data"]["status"]["my_known_comment_count"], first["data"]["status"]["my_known_comment_count"])

    def test_summary_storage_failure_preserves_successfully_read_post_body(self):
        self.fixture = ReviewFixture()
        with patch.object(InteractionHistory, "summaries", side_effect=sqlite3.OperationalError("fixture summary storage failure")):
            code, value = self.run_cli("post", "501", "--requests", "1")
        self.assertEqual(code, 0)
        self.assertTrue(value["ok"])
        self.assertEqual(value["data"]["body"]["status"], "available")
        self.assertTrue(value["data"]["body"]["text"])
        self.assertEqual(value["data"]["interaction"]["participation"], "unknown")
        self.assertTrue(value["data"]["interaction"]["cache_incomplete"])

    def test_cross_root_relation_never_confirms_a_round_or_answer(self):
        self.store().capture("101", [
            {"id": "601", "post_id": "501", "root_id": "601", "author": {"uid": "101"}, "text": "my root"},
            {"id": "602", "post_id": "501", "root_id": "601", "author": {"uid": "202"}, "reply_to_id": "601", "text": "input"},
            {"id": "701", "post_id": "501", "root_id": "999", "author": {"uid": "101"}, "reply_to_id": "602", "text": "contradictory root"},
        ], "public", {"route": "fixture", "observed_at": 100, "identity_verified_this_read": True})
        state = self.store().status(Scope("101", "501", "202"))
        self.assertEqual(state["completed_round_count"], 0)
        pending = next(row for row in state["pending"] if row["incoming_comment_id"] == "602")
        self.assertNotEqual(pending["state"], "answered")

    def assert_incomplete_root_scope(self, nodes, root, reply):
        self.store().capture("101", nodes, "public",
            {"route": "fixture", "observed_at": 100, "identity_verified_this_read": True})
        for scope in (Scope("101", "501", root_id=root), Scope("101", "501", "202", root)):
            with self.subTest(scope=scope):
                state = self.store().status(scope)
                self.assertFalse(state["ancestry_complete"])
                self.assertIn(reply, state["root_mismatch_ids"])
                self.assertEqual(state["completed_round_count"], 0)

    def test_root_scope_reports_foreign_intermediate_ancestor(self):
        self.assert_incomplete_root_scope([
            {"id": "601", "post_id": "501", "root_id": "601", "author": {"uid": "101"}},
            {"id": "602", "post_id": "501", "root_id": "999", "author": {"uid": "202"}, "reply_to_id": "601"},
            {"id": "701", "post_id": "501", "root_id": "601", "author": {"uid": "101"}, "reply_to_id": "602"},
        ], "601", "701")

    def test_root_scope_reports_inconsistent_terminal_root(self):
        self.assert_incomplete_root_scope([
            {"id": "601", "post_id": "501", "root_id": "999", "author": {"uid": "101"}},
            {"id": "602", "post_id": "501", "root_id": "601", "author": {"uid": "202"}, "reply_to_id": "601"},
            {"id": "701", "post_id": "501", "root_id": "601", "author": {"uid": "101"}, "reply_to_id": "602"},
        ], "601", "701")


if __name__ == "__main__":
    unittest.main()
