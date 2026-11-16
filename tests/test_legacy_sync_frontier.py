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
from paging import decode_resume, encode_resume
import test_interaction_cli as interaction_tests
from test_tools import Response

class LegacyReplyFixture(interaction_tests.InteractionFixture):
    def __call__(self, request, timeout=None):
        value = json.loads(super().__call__(request, timeout).read())
        if urlsplit(request.full_url).path == "/bbs/app/comment/sub/comments":
            value["result"] = {"comments": [
                {"commentid": "602", "userid": "202", "replyid": "601", "replyuserid": "101", "create_at": 20},
                {"commentid": "701", "userid": "101", "replyid": "602", "replyuserid": "202", "create_at": 30}
            ], "has_more": False}
        return Response(value)


class EmptyReplyFixture(interaction_tests.InteractionFixture):
    def __call__(self, request, timeout=None):
        value = json.loads(super().__call__(request, timeout).read())
        if urlsplit(request.full_url).path == "/bbs/app/comment/sub/comments":
            value["result"] = {"comments": [], "has_more": False}
        return Response(value)


class PaginatedKnownFixture(interaction_tests.InteractionFixture):
    def __init__(self):
        super().__init__()
        self.reply_calls = 0

    def __call__(self, request, timeout=None):
        value = json.loads(super().__call__(request, timeout).read())
        if urlsplit(request.full_url).path == "/bbs/app/comment/sub/comments":
            self.reply_calls += 1
            if self.reply_calls == 1:
                value["result"] = {"comments": [
                    {"commentid": "602", "userid": "202", "replyid": "601", "replyuserid": "101", "create_at": 20}
                ], "has_more": True, "lastval": "cursor-one"}
            else:
                value = {"status": "show_captcha"}
        return Response(value)


class LegacySyncFrontierTest(unittest.TestCase):
    setUp = interaction_tests.InteractionCLITest.setUp
    tearDown = interaction_tests.InteractionCLITest.tearDown
    run_cli = interaction_tests.InteractionCLITest.run_cli
    sends = interaction_tests.InteractionCLITest.sends

    def store(self):
        return InteractionHistory(self.folder / "data")

    def seed_references(self, roots, *, reply_to=None):
        nodes = []
        for index, root in enumerate(roots):
            node = {"id": str(700000 + index), "post_id": "501", "root_id": str(root),
                    "author": {"uid": "101"}, "created_at": index + 1}
            if reply_to is not None:
                node["reply_to_id"] = str(reply_to)
            nodes.append(node)
        self.store().capture("101", nodes, "own_list",
            {"route": "/bbs/app/profile/bbs/comment/list", "observed_at": 100, "identity_verified_this_read": True})

    def binding(self):
        return {"post_id": "501", "peer_uid": None, "root_id": None, "protocol": "web", "limit": 20, "sort": "time_aes"}

    def test_preverified_known_root_is_read_before_private_lists(self):
        self.seed_references(["601"])
        self.fixture = LegacyReplyFixture()
        code, result = self.run_cli("interaction", "sync", "501", "--root", "601", "--requests", "2", "--pages", "1")
        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in self.fixture.calls],
                         ["/account/restore_login", "/bbs/app/comment/sub/comments"])
        self.assertEqual(result["data"]["status"]["completed_round_count"], 1)
        self.assertEqual(result["coverage"]["stop_reason"], "page_budget")
        self.assertEqual(result["coverage"]["continuation"]["phase"], "inbox")
        self.assertEqual(result["coverage"]["continuation"]["known_done"], ["601"])
        self.assertFalse(any({"text", "raw_text", "media", "raw"} & set(item)
                             for item in self.store().history(Scope("101", "501"))["items"]))
        self.assertEqual(self.sends(), [])

    def test_peer_scope_keeps_an_unknown_own_reference_eligible(self):
        self.seed_references(["601"])
        self.fixture = LegacyReplyFixture()
        code, result = self.run_cli("interaction", "sync", "501", "--peer", "202", "--requests", "2", "--pages", "1")
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["status"]["participation"], "confirmed")
        self.assertEqual(result["data"]["status"]["completed_round_count"], 1)
        self.assertEqual({item["id"] for item in self.store().history(Scope("101", "501", "202"))["items"]},
                         {"602", "701"})
        self.assertEqual(self.sends(), [])

    def test_new_own_list_reference_precedes_public_page_traversal(self):
        self.fixture = interaction_tests.InteractionFixture()
        code, _ = self.run_cli("interaction", "sync", "501", "--requests", "7", "--pages", "10")
        self.assertEqual(code, 0)
        paths = [call[0] for call in self.fixture.calls]
        self.assertEqual(paths[:3], ["/account/restore_login", "/bbs/app/user/message",
                                     "/bbs/app/profile/bbs/comment/list"])
        self.assertLess(paths.index("/bbs/app/comment/sub/comments"), paths.index("/bbs/app/link/tree"))
        reply = next(call for call in self.fixture.calls if call[0] == "/bbs/app/comment/sub/comments")
        self.assertEqual(reply[3]["root_comment_id"], ["601"])
        self.assertEqual(self.sends(), [])

    def test_completed_known_root_is_not_replayed_after_resume_and_public_rediscovery(self):
        self.seed_references(["601"])
        self.fixture = interaction_tests.InteractionFixture()
        code, first = self.run_cli("interaction", "sync", "501", "--requests", "2", "--pages", "1")
        self.assertEqual(code, 0)
        self.assertEqual(first["coverage"]["continuation"]["known_done"], ["601"])
        code, second = self.run_cli("interaction", "sync", "501", "--requests", "10", "--pages", "10",
                                   "--resume", first["coverage"]["resume"])
        self.assertEqual(code, 0)
        self.assertTrue(second["coverage"]["scan_finished"])
        self.assertIsNone(second["coverage"]["stop_reason"])
        replies = [call for call in self.fixture.calls if call[0] == "/bbs/app/comment/sub/comments"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(self.sends(), [])

    def test_truncated_candidates_are_bounded_and_reported(self):
        self.seed_references([str(1000 + index) for index in range(105)])
        self.fixture = EmptyReplyFixture()
        code, result = self.run_cli("interaction", "sync", "501", "--requests", "100", "--pages", "100")
        self.assertEqual(code, 0)
        self.assertIn("known_root_candidates_truncated", result["coverage"]["gaps"])
        self.assertEqual(result["coverage"]["stop_reason"], "request_budget")
        self.assertEqual(result["coverage"]["budget"]["used_requests"], 100)
        continuation = result["coverage"]["continuation"]
        self.assertEqual(continuation["phase"], "known_replies")
        self.assertEqual(len(continuation["known_done"]), 99)
        self.assertEqual(len(continuation["known_roots"]), 1)
        self.assertEqual(len(continuation["known_done"] + continuation["known_roots"]), 100)
        replies = [call for call in self.fixture.calls if call[0] == "/bbs/app/comment/sub/comments"]
        self.assertEqual(len(replies), 99)
        self.assertEqual(self.sends(), [])

    def test_platform_failure_keeps_known_cursor_and_partial_facts(self):
        self.seed_references(["601"])
        self.fixture = PaginatedKnownFixture()
        code, result = self.run_cli("interaction", "sync", "501", "--requests", "3", "--pages", "10")
        self.assertEqual(code, 1)
        self.assertEqual(result["coverage"]["stop_reason"], "platform_error")
        continuation = result["coverage"]["continuation"]
        self.assertEqual((continuation["phase"], continuation["cursor"]), ("known_replies", "cursor-one"))
        self.assertEqual(continuation["known_roots"], ["601"])
        self.assertEqual(continuation["known_done"], [])
        self.assertEqual(result["coverage"]["pages_read"], 1)
        item = next(row for row in self.store().history(Scope("101", "501"))["items"] if row["id"] == "602")
        self.assertEqual(item["author"]["uid"], "202")
        self.assertNotIn("text", item)
        self.assertEqual(self.sends(), [])

    def test_resume_rejects_forged_completed_and_queued_known_roots_before_http(self):
        self.seed_references(["601"])
        self.fixture = interaction_tests.InteractionFixture()
        first = self.run_cli("interaction", "sync", "501", "--requests", "2", "--pages", "1")[1]
        token = decode_resume(first["coverage"]["resume"], "interaction-sync", "101")
        self.assertEqual(token["state"]["known_done"], ["601"])
        self.store().capture("101", [{"id": "901", "post_id": "999", "root_id": "901", "author": {"uid": "303"}}],
                             "public", {"route": "fixture", "observed_at": 100, "identity_verified_this_read": True})
        self.store().capture("101", [{"id": "903", "post_id": "501", "root_id": "903", "author": {"uid": "101"}}],
                             "public", {"route": "fixture", "observed_at": 100, "identity_verified_this_read": True})
        cases = [
            {**token["state"], "known_done": ["901"]},
            {**token["state"], "known_done": ["903"]},
            {**token["state"], "phase": "known_replies", "known_roots": ["902"], "known_done": []},
        ]
        for state in cases:
            forged = encode_resume("interaction-sync", "101", {**token, "state": state})
            before = len(self.fixture.calls)
            code, value = self.run_cli("interaction", "sync", "501", "--resume", forged)
            self.assertEqual(code, 1)
            self.assertEqual(value["error"]["code"], "XhhConfigError")
            self.assertEqual(len(self.fixture.calls), before)

    def test_legacy_token_without_frontier_fields_still_resumes(self):
        self.fixture = interaction_tests.InteractionFixture()
        legacy = {"phase": "inbox", "offset": 0, "page": 1, "roots": [], "cursor": None,
                  "comments_done": False, "inbox_offset": 0, "own_list_offset": 0}
        token = encode_resume("interaction-sync", "101", {"binding": self.binding(), "state": legacy})
        code, result = self.run_cli("interaction", "sync", "501", "--requests", "2", "--resume", token)
        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in self.fixture.calls],
                         ["/account/restore_login", "/bbs/app/user/message"])
        continuation = result["coverage"]["continuation"]
        self.assertEqual((continuation["known_roots"], continuation["known_done"], continuation["return_phase"]),
                         ([], [], "inbox"))
        self.assertEqual(self.sends(), [])

    def test_public_replies_resume_still_requires_an_observed_root(self):
        self.seed_references(["601"])
        self.fixture = interaction_tests.InteractionFixture()
        state = {"phase": "replies", "offset": 0, "page": 1, "roots": ["601"], "cursor": None,
                 "comments_done": True, "inbox_offset": None, "own_list_offset": None,
                 "known_roots": [], "known_done": [], "return_phase": "inbox"}
        token = encode_resume("interaction-sync", "101", {"binding": self.binding(), "state": state})
        code, value = self.run_cli("interaction", "sync", "501", "--resume", token)
        self.assertEqual(code, 1)
        self.assertEqual(value["error"]["code"], "XhhConfigError")
        self.assertEqual(self.fixture.calls, [])


if __name__ == "__main__":
    unittest.main()
