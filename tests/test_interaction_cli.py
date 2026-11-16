from __future__ import annotations

import contextlib
import io
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(os.environ.get("XHH_TEST_SKILL_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT / "scripts"), str(Path(__file__).resolve().parent)]

import xhh
from interaction_history import InteractionHistory, Scope, database
from paging import decode_resume, encode_resume
import test_tools
from test_tools import PlatformFixture, Response


class InteractionFixture(PlatformFixture):
    def __init__(self):
        super().__init__()
        self.public_own = False
        self.malformed_own = False
        self.wrong_own = False
        self.bad_second_page = False
        self.stop_path = None
        self.stop_response = {"status": "show_captcha"}
        self.incoming = {"message_id": "event-one", "comment_a_id": "602", "comment_b_id": "601", "root_comment_id": "601",
            "linkid": "501", "comment_a_userid": "202", "comment_b_userid": "101",
            "comment_a_text": "  full incoming\n" * 500, "comment_b_text": "  own prior\n" * 500}

    def __call__(self, request, timeout=None):
        value = json.loads(super().__call__(request, timeout).read())
        path = urlsplit(request.full_url).path
        query = parse_qs(urlsplit(request.full_url).query)
        if value.get("status") != "ok":
            return Response(value)
        if self.stop_path == path:
            return Response(self.stop_response)
        if path == "/bbs/app/user/message":
            value["result"] = {"entries": [self.incoming], "message_unread_num": {"comment": 1}, "has_more": False}
        elif path == "/bbs/app/profile/bbs/comment/list":
            value["result"] = [] if query.get("offset") != ["0"] else [{"commentid": "701", "linkid": "501",
                "root_comment_id": "601", "replyid": "602", "userid": "999" if self.wrong_own else "101", "text": "own list body", "create_at": 50}]
            if self.malformed_own:
                value["result"] = {"comments": []}
        elif path == "/bbs/app/link/tree":
            page = int(query.get("page", ["1"])[0])
            nodes = [{"commentid": "601", "userid": "101", "text": "root", "child_num": 2, "has_more": True},
                {"commentid": "602", "userid": "202", "replyid": "601", "replyuserid": "101", "text": "  public peer\n" * 500}]
            if self.public_own:
                nodes.append({"commentid": "701", "userid": "101", "replyid": "602", "text": "public edited", "modify_at": 70, "create_at": 50})
            value["result"].update(comments=[{"comment": nodes}] if page == 1 else [{"comment": [
                {"commentid": "801", "userid": "303", "text": "unrelated", "child_num": 0}]}], total_page=2)
            if page == 2 and self.bad_second_page:
                value["result"]["comments"] = {"malformed": True}
        elif path == "/bbs/app/comment/sub/comments":
            value["result"] = {"comments": [{"commentid": "701", "userid": "101", "replyid": "602", "text": "public edited", "modify_at": 70, "create_at": 50}], "has_more": False}
        elif path in ("/bbs/app/topic/feeds", "/bbs/app/hot_news/main_list"):
            value["result"] = {"links": [{"linkid": "501"}, {"linkid": "999"}], "has_more": False}
        return Response(value)


class InteractionCLITest(unittest.TestCase):
    setUp = test_tools.ToolsTest.setUp
    tearDown = test_tools.ToolsTest.tearDown
    run_cli = test_tools.ToolsTest.run_cli
    sends = test_tools.ToolsTest.sends
    comment = test_tools.ToolsTest.comment

    def store(self):
        return InteractionHistory(self.folder / "data")

    def fixture_for_interactions(self):
        self.fixture = InteractionFixture()

    def test_status_and_history_do_not_construct_client_or_write_missing_database(self):
        for action in ("status", "history"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = xhh.main(["--config", str(self.config), "--data-dir", str(self.folder / "missing"),
                    "interaction", action, "501"], client_factory=lambda config: self.fail("local query created network client"))
            value = json.loads(output.getvalue())
            self.assertEqual((code, value["version"], value["ok"]), (0, 2, True))
        self.assertFalse((self.folder / "missing").exists())

    def test_config_only_post_one_request_keeps_unknown_without_establishing_private_namespace(self):
        self.fixture_for_interactions()
        code, value = self.run_cli("post", "501", "--requests", "1")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertEqual(value["data"]["interaction"]["participation"], "unknown")
        self.assertFalse(value["data"]["source"]["identity_verified_this_read"])
        self.assertFalse(value["coverage"]["interaction_capture"]["pages"][0]["persisted"])
        self.assertFalse((self.folder / "data").exists())

    def test_receipt_then_ordinary_one_request_public_readback_persists_exact_round(self):
        self.fixture_for_interactions()
        code, value = self.run_cli("reply", "501", "601", "602", "--text-file", str(self.text), "--operation-id", "incoming-602", "--confirm")
        self.assertEqual(code, 0)
        self.assertEqual(value["data"]["status"], "returned")
        self.assertEqual(self.store().status(Scope("101", "501"))["participation"], "submitted")
        self.fixture.public_own = True
        before = len(self.fixture.calls)
        code, value = self.run_cli("post", "501", "--requests", "1")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.fixture.calls) - before, 1)
        self.assertEqual(value["data"]["interaction"]["completed_round_count"], 1)
        self.assertFalse(value["data"]["source"]["identity_verified_this_read"])
        items = self.store().history(Scope("101", "501"))["items"]
        own = next(item for item in items if item["id"] == "701")
        self.assertEqual(own["interaction_type"], "reply")
        self.assertNotIn("text", own)
        self.assertEqual(own["reply_to_id"], "602")
        self.assertTrue(own["sources"]["public:/bbs/app/link/tree"]["kind"] == "public")
        self.assertFalse(own["sources"]["public:/bbs/app/link/tree"]["identity_verified_this_read"])
        with database(self.store().path) as connection:
            context = connection.execute("SELECT * FROM interaction_write_context").fetchone()
        submitted = {"link_id": "501", "root_id": "601", "reply_id": "602", "text": self.text.read_text(encoding="utf-8"), "imgs": "", "is_cy": "0"}
        digest = hashlib.sha256(json.dumps(submitted, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(context["submission_fingerprint"], digest)
        self.assertEqual(context["request_protocol"], "web")

    def test_verified_inbox_keeps_raw_contract_and_deduplicates_changed_notification_ids(self):
        self.fixture_for_interactions()
        code, value = self.run_cli("inbox")
        self.assertEqual(code, 0)
        self.assertEqual(value["data"]["entries"], [self.fixture.incoming])
        self.assertEqual(value["data"]["message_unread_num"], {"comment": 1})
        self.assertEqual(value["coverage"]["budget"]["used_requests"], 2)
        self.assertTrue(value["coverage"]["identity_verified_this_read"])
        self.fixture.incoming["message_id"] = "changed-event"
        self.assertEqual(self.run_cli("inbox")[0], 0)
        status = self.store().status(Scope("101", "501"))
        self.assertEqual(len(status["pending"]), 1)
        self.assertEqual(status["pending"][0]["operation_id"], "incoming-602")
        item = next(row for row in self.store().history(Scope("101", "501"))["items"] if row["id"] == "602")
        self.assertFalse({"text", "raw_text", "media", "raw"} & item.keys())
        self.assertEqual(item["author"]["uid"], "202")
        self.assertEqual(self.sends(), [])

    def test_private_uid_mismatch_precedes_ingestion_and_reads(self):
        self.fixture_for_interactions()
        self.fixture.identity = "999"
        for command in ("inbox", "my-comments", "interaction"):
            arguments = [command] if command != "interaction" else [command, "sync", "501"]
            self.assertEqual(self.run_cli(*arguments)[1]["error"]["code"], "account_identity_not_verified")
        self.assertTrue(all(call[0] == "/account/restore_login" for call in self.fixture.calls))
        self.assertFalse((self.folder / "data").exists())

    def test_raw_own_comment_shape_and_author_mismatch_are_errors(self):
        self.fixture_for_interactions()
        self.fixture.malformed_own = True
        self.assertEqual(self.run_cli("my-comments")[1]["error"]["code"], "invalid_my_comments_response")
        self.fixture.malformed_own = False
        self.fixture.wrong_own = True
        self.assertEqual(self.run_cli("my-comments")[1]["error"]["code"], "own_comment_uid_mismatch")
        self.assertFalse((self.folder / "data").exists())

    def test_own_list_confirms_participation_without_public_round(self):
        self.fixture_for_interactions()
        code, value = self.run_cli("my-comments")
        self.assertEqual(code, 0)
        self.assertIsInstance(value["data"], list)
        status = self.store().status(Scope("101", "501"))
        self.assertEqual((status["participation"], status["my_known_comment_count"], status["completed_round_count"]), ("confirmed", 1, 0))
        self.assertEqual(status["pending"][0]["incoming_comment_id"], "602")
        self.assertEqual(status["pending"][0]["state"], "replied_visibility_unverified")

    def test_feed_and_hot_add_small_batched_local_summaries_with_no_extra_http(self):
        self.fixture_for_interactions()
        self.comment()
        original = InteractionHistory.summaries
        for arguments in (("feed", "123"), ("hot",)):
            before = len(self.fixture.calls)
            with patch.object(InteractionHistory, "summaries", autospec=True, side_effect=original) as summaries:
                code, value = self.run_cli(*arguments, "--requests", "1")
                self.assertEqual(summaries.call_count, 1)
            self.assertEqual(code, 0)
            self.assertEqual(len(self.fixture.calls) - before, 1)
            known, unknown = value["data"]["items"]
            self.assertEqual(known["interaction"]["participation"], "submitted")
            self.assertEqual(unknown["interaction"]["participation"], "unknown")
            self.assertNotIn("peer_summaries", known["interaction"])
            self.assertNotIn("pending", known["interaction"])

    def test_sync_reads_private_public_and_deep_pages_with_bounded_resume(self):
        self.fixture_for_interactions()
        code, first = self.run_cli("interaction", "sync", "501", "--requests", "3", "--pages", "10")
        self.assertEqual(code, 0)
        self.assertEqual(first["coverage"]["budget"]["used_requests"], 3)
        self.assertEqual(first["coverage"]["stop_reason"], "request_budget")
        self.assertEqual(first["coverage"]["continuation"]["phase"], "known_replies")
        self.assertIsNotNone(first["coverage"]["resume"])
        self.assertEqual(first["data"]["status"]["completed_round_count"], 0)
        code, second = self.run_cli("interaction", "sync", "501", "--requests", "10", "--pages", "10", "--resume", first["coverage"]["resume"])
        self.assertEqual(code, 0)
        self.assertTrue(second["coverage"]["scan_finished"])
        self.assertEqual(second["coverage"]["completion"], "unknown")
        self.assertIsNone(second["coverage"]["resume"])
        self.assertFalse(second["coverage"]["history_complete"])
        self.assertEqual(second["data"]["status"]["completed_round_count"], 1)
        self.assertEqual(next(row for row in second["data"]["status"]["pending"] if row["incoming_comment_id"] == "602")["state"], "answered")
        self.assertEqual(self.sends(), [])

    def test_sync_resume_rejects_account_post_peer_root_limit_protocol_before_http(self):
        self.fixture_for_interactions()
        first = self.run_cli("interaction", "sync", "501", "--pages", "1")[1]
        token = first["coverage"]["resume"]
        for arguments in (("502",), ("501", "--peer", "999"), ("501", "--root", "999"), ("501", "--limit", "21")):
            before = len(self.fixture.calls)
            code, value = self.run_cli("interaction", "sync", *arguments, "--resume", token)
            self.assertEqual(code, 1)
            self.assertEqual(value["error"]["code"], "configuration_error")
            self.assertEqual(len(self.fixture.calls), before)
        original = json.loads(self.config.read_text())
        for field, value in (("heybox_id", "102"), ("protocol_mode", "app")):
            self.config.write_text(json.dumps({**original, field: value}))
            before = len(self.fixture.calls)
            self.assertEqual(self.run_cli("interaction", "sync", "501", "--resume", token)[0], 1)
            self.assertEqual(len(self.fixture.calls), before)
        self.config.write_text(json.dumps(original))

    def test_sync_resume_rejects_forged_unknown_and_foreign_roots_before_http(self):
        self.fixture_for_interactions()
        first = self.run_cli("interaction", "sync", "501", "--pages", "1")[1]
        token = decode_resume(first["coverage"]["resume"], "interaction-sync", "101")
        self.store().capture("101", [{"id": "901", "post_id": "999", "root_id": "901", "author": {"uid": "303"},
                                      "text": "foreign post root"}], "public",
                             {"route": "fixture", "observed_at": 100, "identity_verified_this_read": True})
        for root in ("901", "902"):
            state = {**token["state"], "phase": "replies", "roots": [root], "cursor": None, "comments_done": True}
            forged = encode_resume("interaction-sync", "101", {**token, "state": state})
            before = len(self.fixture.calls)
            code, value = self.run_cli("interaction", "sync", "501", "--resume", forged)
            self.assertEqual(code, 1)
            self.assertEqual(value["error"]["code"], "configuration_error")
            self.assertEqual(len(self.fixture.calls), before)

    def test_sync_reaches_target_post_before_exhausting_large_private_lists(self):
        class BusyPersonalLists(InteractionFixture):
            def __call__(self, request, timeout=None):
                value = json.loads(super().__call__(request, timeout).read())
                path = urlsplit(request.full_url).path
                query = parse_qs(urlsplit(request.full_url).query)
                offset = int(query.get("offset", ["0"])[0])
                if path == "/bbs/app/user/message":
                    value["result"] = {"entries": [{**self.incoming, "message_id": "unrelated-" + str(offset),
                        "linkid": "999", "comment_a_id": str(10002 + offset), "comment_b_id": str(10001 + offset),
                        "root_comment_id": str(10001 + offset)}], "has_more": True}
                elif path == "/bbs/app/profile/bbs/comment/list":
                    value["result"] = [{"commentid": str(20000 + offset), "linkid": "998", "userid": "101",
                                        "text": "another post", "root_comment_id": str(20000 + offset)}]
                return Response(value)
        self.fixture = BusyPersonalLists()
        code, value = self.run_cli("interaction", "sync", "501", "--requests", "4", "--pages", "3")
        self.assertEqual(code, 0)
        self.assertEqual(value["coverage"]["budget"]["used_requests"], 4)
        self.assertEqual(value["data"]["status"]["participation"], "confirmed")
        self.assertTrue(any(call[0] == "/bbs/app/link/tree" for call in self.fixture.calls))
        self.assertEqual(self.sends(), [])

    def test_sync_resume_cannot_replace_requested_root_with_a_known_sibling(self):
        self.fixture_for_interactions()
        first = self.run_cli("interaction", "sync", "501", "--root", "601", "--pages", "1")[1]
        token = decode_resume(first["coverage"]["resume"], "interaction-sync", "101")
        self.store().capture("101", [{"id": "903", "post_id": "501", "root_id": "903", "author": {"uid": "303"},
                                      "text": "same post different root"}], "public",
                             {"route": "fixture", "observed_at": 100, "identity_verified_this_read": True})
        state = {**token["state"], "phase": "replies", "roots": ["903"], "cursor": None, "comments_done": True}
        forged = encode_resume("interaction-sync", "101", {**token, "state": state})
        before = len(self.fixture.calls)
        code, value = self.run_cli("interaction", "sync", "501", "--root", "601", "--resume", forged)
        self.assertEqual(code, 1)
        self.assertEqual(value["error"]["code"], "configuration_error")
        self.assertEqual(len(self.fixture.calls), before)

    def test_sync_malformed_later_page_retains_imported_facts_and_failed_position(self):
        self.fixture_for_interactions()
        self.fixture.bad_second_page = True
        code, value = self.run_cli("interaction", "sync", "501")
        self.assertEqual(code, 1)
        self.assertEqual(value["coverage"]["stop_reason"], "platform_error")
        self.assertEqual(value["coverage"]["continuation"]["phase"], "comments")
        self.assertEqual(value["coverage"]["continuation"]["page"], 2)
        self.assertEqual(value["data"]["status"]["completed_round_count"], 1)
        self.assertIsNone(value["coverage"]["resume"])
        self.assertEqual(self.sends(), [])

    def test_sync_captcha_retains_pages_and_does_not_switch_protocol(self):
        self.fixture_for_interactions()
        self.fixture.stop_path = "/bbs/app/link/tree"
        code, value = self.run_cli("interaction", "sync", "501")
        self.assertEqual(code, 1)
        self.assertEqual(value["coverage"]["stop_reason"], "platform_error")
        self.assertEqual(value["data"]["status"]["my_known_comment_count"], 2)
        self.assertEqual(value["coverage"]["continuation"]["phase"], "comments")
        self.assertIsNone(value["coverage"]["resume"])
        self.assertEqual(self.sends(), [])

    def test_sync_hard_blocks_never_return_a_resume_token(self):
        cases = (
            ({"status": "show_captcha"}, "captcha_required"),
            ({"status": "HTTP_429"}, "rate_limited"),
            (
                {"status": "login", "msg": "login required"},
                "session_expired",
            ),
        )
        for response, expected_code in cases:
            with self.subTest(code=expected_code):
                self.fixture_for_interactions()
                self.fixture.stop_path = "/bbs/app/link/tree"
                self.fixture.stop_response = response

                code, value = self.run_cli(
                    "interaction", "sync", "501",
                )

                self.assertEqual(code, 1)
                self.assertEqual(
                    value["coverage"]["error"]["code"],
                    expected_code,
                )
                self.assertIsNone(value["coverage"]["resume"])
                self.assertEqual(
                    value["coverage"]["continuation"]["phase"],
                    "comments",
                )
                self.assertEqual(
                    value["data"]["status"]["my_known_comment_count"],
                    2,
                )
                self.assertEqual(self.sends(), [])

    def test_sync_request_one_page_and_time_budget_stop_without_post(self):
        self.fixture_for_interactions()
        code, value = self.run_cli("interaction", "sync", "501", "--requests", "1")
        self.assertEqual(code, 0)
        self.assertEqual(value["coverage"]["stop_reason"], "request_budget")
        self.assertEqual(value["coverage"]["pages_read"], 0)
        code, value = self.run_cli("interaction", "sync", "501", "--pages", "1")
        self.assertEqual(value["coverage"]["stop_reason"], "page_budget")
        from paging import Budget
        original_call = Budget.call
        def expire_after_identity(budget, client, function):
            result = original_call(budget, client, function)
            budget.deadline = 0
            return result
        with patch.object(Budget, "call", expire_after_identity):
            code, value = self.run_cli("interaction", "sync", "501")
        self.assertEqual(code, 0)
        self.assertEqual(value["coverage"]["stop_reason"], "time_budget")
        self.assertEqual(value["coverage"]["budget"]["used_requests"], 1)
        self.assertEqual(self.sends(), [])

    def test_ordinary_verified_snapshot_and_thread_automatically_capture_new_pages(self):
        self.fixture_for_interactions()
        code, value = self.run_cli("thread", "501", "601", "--around", "701", "--pages", "2", "--snapshot", "thread")
        self.assertEqual(code, 0)
        self.assertTrue(value["coverage"]["interaction_capture"]["complete"])
        self.assertEqual(self.store().status(Scope("101", "501"))["completed_round_count"], 1)
        self.assertTrue(self.store().trusted("101"))

    def test_capture_failure_preserves_read_result_with_explicit_cache_gap(self):
        self.fixture_for_interactions()
        self.comment()
        with patch.object(InteractionHistory, "capture", side_effect=OSError("fixture disk full")):
            code, value = self.run_cli("post", "501", "--requests", "1")
        self.assertEqual(code, 0)
        self.assertIn("body", value["data"])
        self.assertFalse(value["coverage"]["interaction_capture"]["complete"])
        self.assertEqual(value["coverage"]["interaction_capture"]["pages"][0]["reason"], "interaction_cache_incomplete")

    def test_standalone_reply_page_cannot_poison_post_membership_from_caller_claim(self):
        self.fixture_for_interactions()
        self.run_cli("inbox")
        self.run_cli("comments", "501", "--requests", "1")
        code, value = self.run_cli("replies", "601", "--post-id", "999", "--requests", "1")
        self.assertEqual(code, 0)
        self.assertEqual(value["coverage"]["interaction_capture"]["pages"][0]["reason"], "root_membership_not_established")
        self.assertEqual(self.store().history(Scope("101", "999"))["items"], [])
        self.assertEqual(self.run_cli("replies", "601", "--post-id", "501", "--requests", "1")[0], 0)
        self.assertEqual(self.store().status(Scope("101", "501"))["completed_round_count"], 1)

    def test_account_info_establishes_historical_namespace_without_verifying_next_read(self):
        self.fixture_for_interactions()
        self.assertEqual(self.run_cli("account-info")[0], 0)
        self.assertTrue(self.store().trusted("101"))
        code, value = self.run_cli("post", "501", "--requests", "1")
        self.assertEqual(code, 0)
        self.assertTrue(value["data"]["interaction"]["identity_established"])
        self.assertFalse(value["data"]["source"]["identity_verified_this_read"])

    def test_private_single_request_budget_never_reads_personal_data_or_creates_state(self):
        self.fixture_for_interactions()
        code, value = self.run_cli("inbox", "--requests", "1")
        self.assertEqual(code, 1)
        self.assertEqual(value["coverage"]["stop_reason"], "request_budget")
        self.assertEqual([call[0] for call in self.fixture.calls], ["/account/restore_login"])
        self.assertFalse((self.folder / "data").exists())

    def test_sync_resume_login_failure_drops_resume_and_keeps_continuation(self):
        self.fixture_for_interactions()
        first = self.run_cli("interaction", "sync", "501", "--requests", "3")[1]
        previous_state = decode_resume(
            first["coverage"]["resume"], "interaction-sync", "101",
        )["state"]
        self.fixture.identity = "999"
        before = len(self.fixture.calls)
        code, value = self.run_cli("interaction", "sync", "501", "--resume", first["coverage"]["resume"])
        self.assertEqual(code, 1)
        self.assertEqual(value["error"]["code"], "account_identity_not_verified")
        self.assertIsNone(value["coverage"]["resume"])
        self.assertEqual(value["coverage"]["continuation"], previous_state)
        self.assertEqual(value["data"]["status"]["my_known_comment_count"], first["data"]["status"]["my_known_comment_count"])
        self.assertFalse(value["data"]["identity_verified_this_read"])
        self.assertEqual(len(self.fixture.calls) - before, 1)

    def test_sync_storage_failure_preserves_full_unsaved_page_and_retry_position(self):
        self.fixture_for_interactions()
        with patch.object(InteractionHistory, "capture", side_effect=OSError("fixture disk failure")):
            code, value = self.run_cli("interaction", "sync", "501")
        self.assertEqual(code, 1)
        self.assertEqual(value["coverage"]["stop_reason"], "storage_error")
        self.assertEqual(value["error"]["code"], "interaction_cache_incomplete")
        self.assertEqual(value["data"]["unpersisted_page"]["entries"], [self.fixture.incoming])
        self.assertEqual(value["coverage"]["continuation"]["offset"], 0)
        self.assertIsNotNone(value["coverage"]["resume"])
        self.assertEqual(self.sends(), [])

    def test_sync_known_root_storage_failure_keeps_retry_position(self):
        self.fixture_for_interactions()
        first = self.run_cli(
            "interaction", "sync", "501", "--requests", "3", "--pages", "10",
        )[1]
        before = len(self.fixture.calls)
        with patch.object(
            InteractionHistory,
            "root_reference_valid",
            side_effect=[
                True,
                sqlite3.OperationalError("database is locked"),
            ],
        ):
            code, value = self.run_cli(
                "interaction", "sync", "501",
                "--requests", "10", "--pages", "10",
                "--resume", first["coverage"]["resume"],
            )

        self.assertEqual(code, 1)
        self.assertEqual(value["coverage"]["stop_reason"], "storage_error")
        self.assertEqual(
            value["coverage"]["error"]["code"],
            "interaction_cache_incomplete",
        )
        self.assertEqual(
            value["coverage"]["continuation"]["phase"],
            "known_replies",
        )
        self.assertIsNotNone(value["coverage"]["resume"])
        self.assertIn("status", value["data"])
        self.assertEqual(len(self.fixture.calls) - before, 1)
        self.assertEqual(self.sends(), [])

    def test_packaged_cli_local_recovery_uses_stdlib_and_cannot_contact_a_server(self):
        self.fixture_for_interactions()
        self.comment()
        with database(self.store().path, write=True) as connection:
            connection.execute("UPDATE interaction_write_context SET projected_at=NULL")
            connection.execute("DELETE FROM interaction_comments")
        isolated = self.folder / "copied skill"
        shutil.copytree(ROOT / "scripts", isolated / "scripts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.config.write_text(json.dumps({"pkey": "fixture-secret-only", "heybox_id": "101", "protocol_mode": "web", "api_base": "https://127.0.0.1:1"}))
        environment = {key: value for key, value in os.environ.items() if not key.startswith("XHH_") and key != "PYTHONPATH"}
        result = subprocess.run([sys.executable, "-I", "-S", "-X", "utf8", str(isolated / "scripts/xhh.py"),
            "--config", str(self.config), "--data-dir", str(self.folder / "data"), "interaction", "history", "501"],
            cwd=self.folder, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        value = json.loads(result.stdout)
        self.assertEqual(value["version"], 2)
        self.assertEqual(value["data"]["items"][0]["id"], "701")
        self.assertTrue(value["data"]["status"]["projection_complete"])


if __name__ == "__main__":
    unittest.main()
