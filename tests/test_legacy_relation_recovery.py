from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(os.environ.get("XHH_TEST_SKILL_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT / "scripts"), str(Path(__file__).resolve().parent)]

from interaction_history import InteractionHistory, Scope, database
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


class CallerPageFixture(LegacyRootFixture):
    def __call__(self, request, timeout=None):
        value = json.loads(super().__call__(request, timeout).read())
        path = urlsplit(request.full_url).path
        if path == "/bbs/app/link/tree":
            value["result"].update(comments=[{"comment": [
                {"commentid": "601", "userid": "202", "child_num": 2, "has_more": True}
            ]}], total_page=1)
        elif path == "/bbs/app/comment/sub/comments":
            cursor = parse_qs(urlsplit(request.full_url).query).get("lastval")
            if cursor:
                value["result"] = {"comments": [
                    {"commentid": "602", "userid": "202", "replyid": "601", "replyuserid": "202", "create_at": 20}
                ], "has_more": False}
            else:
                value["result"] = {"comments": [
                    {"commentid": "701", "userid": "101", "replyid": "602", "replyuserid": "202", "create_at": 30}
                ], "has_more": True, "lastval": "cursor-one"}
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

    def test_legacy_zero_is_an_explicit_incomplete_lower_bound(self):
        self.seed_old_comment()
        state = self.store().status(Scope("101", "501"))
        self.assertEqual(state["completed_round_count"], 0)
        self.assertEqual(state.get("count_semantics"), "public_verified_lower_bound")
        self.assertEqual(state.get("discussion_status"), "unknown")
        self.assertEqual(state.get("relation_completeness"), "partial")
        self.assertEqual(state.get("unattributed_own_comment_count"), 1)
        self.assertIs(state.get("own_reply_relations_complete"), False)
        self.assertFalse(state["history_complete"])

    def test_peer_query_keeps_unknown_own_relation_warning(self):
        self.seed_old_comment()
        state = self.store().status(Scope("101", "501", "202"))
        self.assertEqual(state["completed_round_count"], 0)
        self.assertEqual(state.get("unattributed_own_comment_ids"), ["701"])
        self.assertEqual(state.get("relation_completeness"), "partial")
        self.assertEqual(state.get("discussion_status"), "unknown")

    def test_lightweight_summaries_carry_relation_uncertainty(self):
        self.seed_old_comment()
        summaries = self.store().summaries("101", ["501", "999"])
        self.assertEqual(summaries["501"].get("count_semantics"), "public_verified_lower_bound")
        self.assertEqual(summaries["501"].get("relation_completeness"), "partial")
        self.assertEqual(summaries["501"].get("unattributed_own_comment_count"), 1)
        self.assertEqual(summaries["999"].get("discussion_status"), "unknown")
        self.assertEqual(summaries["999"].get("relation_completeness"), "unknown")
        self.assertEqual(self.fixture.calls, [])

    def test_sync_reference_does_not_become_public_root_membership(self):
        self.seed_old_comment()
        scope = Scope("101", "501")
        self.assertTrue(self.store().root_reference_valid(scope, "601"))
        self.assertIsNone(self.store().root_post("101", "601"))
        self.assertEqual(self.store().root_candidates(scope)["root_ids"], ["601"])
        self.assertFalse(self.store().root_reference_valid(Scope("101", "999"), "601"))
        self.assertFalse(self.store().root_reference_valid(Scope("101", "501", root_id="609"), "601"))

    def test_foreign_observed_root_blocks_private_reference(self):
        self.seed_old_comment()
        self.store().capture("101", [{"id": "601", "post_id": "999", "root_id": "601", "author": {"uid": "303"}}],
            "public", {"route": "/bbs/app/link/tree", "observed_at": 200, "identity_verified_this_read": True})
        scope = Scope("101", "501")
        self.assertFalse(self.store().root_reference_valid(scope, "601"))
        self.assertEqual(self.store().root_candidates(scope)["root_ids"], [])
        self.assertIn("root_reference_conflict:601", self.store().root_candidates(scope)["gaps"])

    def test_caller_only_public_reference_cannot_seed_recovery(self):
        self.fixture = LegacyRootFixture()
        self.store().capture("101", [{"id": "701", "post_id": "501", "root_id": "601", "author": {"uid": "101"}}],
            "public", {"route": "/bbs/app/comment/sub/comments", "post_id_source": "caller", "observed_at": 100,
                       "identity_verified_this_read": True})
        scope = Scope("101", "501")
        self.assertFalse(self.store().root_reference_valid(scope, "601"))
        self.assertEqual(self.store().root_candidates(scope)["root_ids"], [])

    def test_caller_replies_page_preserves_verified_reference_for_followup_sync(self):
        self.seed_old_comment()
        self.fixture = CallerPageFixture()
        self.assertEqual(self.run_cli("comments", "501", "--requests", "1")[0], 0)
        self.assertEqual(self.run_cli("replies", "601", "--post-id", "501", "--requests", "1")[0], 0)
        self.assertTrue(self.store().root_reference_valid(Scope("101", "501"), "601"))
        self.fixture.calls.clear()
        code, result = self.run_cli("interaction", "sync", "501", "--root", "601", "--requests", "3", "--pages", "10")
        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in self.fixture.calls], [
            "/account/restore_login", "/bbs/app/comment/sub/comments", "/bbs/app/comment/sub/comments"
        ])
        self.assertEqual(result["data"]["status"]["completed_round_count"], 1)
        self.assertIs(result["data"]["status"]["own_reply_relations_complete"], True)

    def test_verified_own_list_repairs_an_old_caller_only_root_claim(self):
        self.seed_old_comment()
        with database(self.store().path, write=True) as connection:
            row = connection.execute("SELECT field_evidence_json,source_json FROM interaction_comments WHERE uid='101' AND comment_id='701'").fetchone()
            fields, sources = json.loads(row[0]), json.loads(row[1])
            fields["root_id"] = {"source": "public", "observed_at": 200, "platform_updated_at": None}
            sources["public:/bbs/app/comment/sub/comments"] = {"kind": "public", "route": "/bbs/app/comment/sub/comments",
                "post_id_source": "caller", "observed_at": 200, "first_observed_at": 200, "last_observed_at": 200}
            connection.execute("UPDATE interaction_comments SET root_id='901',field_evidence_json=?,source_json=? WHERE uid='101' AND comment_id='701'",
                               (json.dumps(fields), json.dumps(sources)))
        self.store().capture("101", [{"id": "701", "post_id": "501", "root_id": "601", "author": {"uid": "101"}}],
            "own_list", {"route": "/bbs/app/profile/bbs/comment/list", "observed_at": 300, "identity_verified_this_read": True})
        self.assertTrue(self.store().root_reference_valid(Scope("101", "501"), "601"))
        self.assertFalse(self.store().root_reference_valid(Scope("101", "501"), "901"))

    def test_caller_context_at_same_timestamp_keeps_stronger_root_evidence(self):
        self.seed_old_comment()
        node = {"id": "701", "post_id": "501", "root_id": "601", "author": {"uid": "101"}}
        self.store().capture("101", [node], "public", {"route": "/bbs/app/comment/sub/comments",
            "post_id_source": "verified_own_list_reference", "observed_at": 200, "identity_verified_this_read": True})
        self.store().capture("101", [{**node, "root_id": "901"}], "public", {"route": "/bbs/app/comment/sub/comments",
            "post_id_source": "caller", "observed_at": 200, "identity_verified_this_read": True})
        self.assertTrue(self.store().root_reference_valid(Scope("101", "501"), "601"))
        self.assertFalse(self.store().root_reference_valid(Scope("101", "501"), "901"))
        item = self.store().history(Scope("101", "501"))["items"][0]
        self.assertEqual(item["root_id"], "601")
        self.assertIs(item["field_evidence"]["root_id"]["caller_context"], False)

    def test_sync_reads_known_legacy_root_before_unrelated_post_pages(self):
        self.seed_old_comment()
        before = self.store().status(Scope("101", "501"))
        self.assertEqual(before["participation"], "confirmed")
        self.assertEqual(before["completed_round_count"], 0)
        code, result = self.run_cli("interaction", "sync", "501", "--root", "601", "--requests", "2", "--pages", "1")
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["status"]["completed_round_count"], 1)
        self.assertIs(result["data"]["status"].get("own_reply_relations_complete"), True)
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
