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
from test_tools import ToolsTest, Response


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
    setUp = ToolsTest.setUp
    tearDown = ToolsTest.tearDown
    run_cli = ToolsTest.run_cli

    def store(self):
        return InteractionHistory(self.folder / "data")

    def own(self):
        return next(row for row in self.store().history(Scope("101", "501"))["items"] if row["id"] == "701")

    def test_actual_own_comment_images_are_retained(self):
        self.fixture = ReviewFixture()
        code, value = self.run_cli("my-comments")
        self.assertEqual(code, 0)
        self.assertTrue(value["ok"])
        self.assertIn("https://imgheybox.max-c.com/own-list.png", [asset["url"] for asset in self.own()["media"]])

    def test_explicit_text_edit_removes_old_inline_image(self):
        self.fixture = ReviewFixture()
        self.assertEqual(self.run_cli("account-info")[0], 0)
        self.assertEqual(self.run_cli("comments", "501", "--requests", "1")[0], 0)
        self.assertIn("https://imgheybox.max-c.com/inline.png", [asset["url"] for asset in self.own()["media"]])
        self.fixture.public_text = "image removed"
        self.assertEqual(self.run_cli("comments", "501", "--requests", "1")[0], 0)
        self.assertNotIn("https://imgheybox.max-c.com/inline.png", [asset["url"] for asset in self.own()["media"]])

    def test_sync_identity_persistence_failure_keeps_prior_status_and_resume(self):
        self.fixture = ReviewFixture()
        code, first = self.run_cli("interaction", "sync", "501", "--requests", "3", "--pages", "2")
        self.assertEqual(code, 0)
        token = first["coverage"]["resume"]
        self.assertTrue(token)
        with patch.object(InteractionHistory, "establish", side_effect=OSError("fixture identity storage failure")):
            code, failed = self.run_cli("interaction", "sync", "501", "--resume", token)
        self.assertEqual(code, 1)
        self.assertEqual(failed["coverage"]["stop_reason"], "storage_error")
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


if __name__ == "__main__":
    unittest.main()
