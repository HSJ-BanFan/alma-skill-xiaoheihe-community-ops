from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(os.environ.get("XHH_TEST_SKILL_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT / "scripts"), str(Path(__file__).resolve().parent)]

from interaction_history import InteractionHistory, Scope
import test_interaction_cli as interaction_tests
from test_tools import Response


class LegacyRootFixture(interaction_tests.InteractionFixture):
    def __call__(self, request, timeout=None):
        value = json.loads(super().__call__(request, timeout).read())
        path = urlsplit(request.full_url).path
        if path == "/bbs/app/link/tree":
            value["result"].update(comments=[{"comment": [
                {"commentid": "801", "userid": "303", "child_num": 0, "has_more": False}
            ]}], total_page=100)
        elif path == "/bbs/app/comment/sub/comments":
            value["result"] = {"comments": [
                {"commentid": "602", "userid": "202", "replyid": "601", "replyuserid": "101", "create_at": 20},
                {"commentid": "701", "userid": "101", "replyid": "602", "replyuserid": "202", "create_at": 30}
            ], "has_more": False}
        return Response(value)


class LegacyRelationRecoveryTest(unittest.TestCase):
    setUp = interaction_tests.InteractionCLITest.setUp
    tearDown = interaction_tests.InteractionCLITest.tearDown
    run_cli = interaction_tests.InteractionCLITest.run_cli
    sends = interaction_tests.InteractionCLITest.sends

    def store(self):
        return InteractionHistory(self.folder / "data")

    def seed_old_comment(self):
        self.fixture = LegacyRootFixture()
        self.store().capture("101", [{"id": "701", "post_id": "501", "root_id": "601", "author": {"uid": "101"}, "created_at": 30}],
            "own_list", {"route": "/bbs/app/profile/bbs/comment/list", "observed_at": 100, "identity_verified_this_read": True})

    def test_sync_reads_known_legacy_root_before_unrelated_post_pages(self):
        self.seed_old_comment()
        before = self.store().status(Scope("101", "501"))
        self.assertEqual(before["participation"], "confirmed")
        self.assertEqual(before["completed_round_count"], 0)
        code, result = self.run_cli("interaction", "sync", "501", "--root", "601", "--requests", "2", "--pages", "1")
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["status"]["completed_round_count"], 1)
        self.assertEqual([call[0] for call in self.fixture.calls], ["/account/restore_login", "/bbs/app/comment/sub/comments"])
        self.assertEqual(self.sends(), [])

    def test_unknown_peer_does_not_hide_a_legacy_root_from_recovery(self):
        self.seed_old_comment()
        code, result = self.run_cli("interaction", "sync", "501", "--peer", "202", "--requests", "2", "--pages", "1")
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["status"]["participation"], "confirmed")
        self.assertEqual(result["data"]["status"]["completed_round_count"], 1)
        own = self.store().history(Scope("101", "501", "202"))["items"]
        self.assertEqual({item["id"] for item in own}, {"602", "701"})
        self.assertEqual(self.sends(), [])


if __name__ == "__main__":
    unittest.main()
