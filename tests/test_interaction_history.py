from __future__ import annotations

import concurrent.futures
from contextlib import closing, contextmanager
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(os.environ.get("XHH_TEST_SKILL_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "scripts"))

from interaction_history import InteractionHistory, Scope, database
from operations import Journal, OperationError, json_text
from xhh_sdk.exceptions import XhhConfigError


def node(cid, *, uid="202", post="501", root="601", parent=None, to=None, text="full text", created=10):
    return {"id": cid, "post_id": post, "root_id": root, "author": {"uid": uid}, "reply_to_id": parent,
            "reply_to_uid": to, "text": text, "created_at": created}


def evidence(at=100, verified=True):
    return {"route": "fixture", "observed_at": at, "identity_verified_this_read": verified}


class InteractionStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "state"
        self.store = InteractionHistory(self.root)
        self.scope = Scope("101", "501")
        self.journal = Journal(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, nodes, kind="public", at=100):
        return self.store.capture("101", nodes, kind, evidence(at))

    def send(self, operation="incoming-602", cid="701", text="sent text", uid="101"):
        return self.journal.execute(uid, operation, "reply", {"link_id": "501", "root_id": "601", "reply_id": "602", "text": text},
                                    lambda: {"comment_id": cid}, identity_verified=True)

    def test_missing_local_reads_have_unknown_counts_and_no_files(self):
        self.assertEqual(self.store.status(self.scope)["participation"], "unknown")
        self.assertEqual(self.store.history(self.scope)["items"], [])
        self.assertEqual(self.store.summaries("101", ["501"])["501"]["participation"], "unknown")
        self.assertFalse(self.store.trusted("101"))
        self.assertFalse(self.root.exists())

    def test_legacy_migration_preserves_all_rows_receipts_and_replays(self):
        self.root.mkdir()
        payload = {"link_id": "501", "text": "legacy body"}
        fingerprint = hashlib.sha256(json_text({"kind": "comment", "payload": payload}).encode()).hexdigest()
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute("CREATE TABLE operations(uid TEXT,operation_id TEXT,fingerprint TEXT,kind TEXT,status TEXT,receipt TEXT,updated_at REAL,PRIMARY KEY(uid,operation_id))")
            rows = [("101", "old", fingerprint, "comment", "returned", '{"comment_id":"701"}', 2),
                    ("101", "closed", "hash", "like", "uncertain", None, 3),
                    ("102", "closed", "hash", "like", "reserved", None, 4)]
            connection.executemany("INSERT INTO operations VALUES(?,?,?,?,?,?,?)", rows)
        self.assertEqual(self.store.status(self.scope)["my_known_comment_count"], 0)
        self.assertEqual(self.journal.read("101", "old")["receipt"]["comment_id"], "701")
        with self.assertRaises(OperationError):
            self.journal.execute("101", "old", "comment", {**payload, "text": "changed"}, lambda: self.fail("POST"))
        self.assertEqual(self.store.history(self.scope)["items"], [])
        replay = self.journal.execute("101", "old", "comment", payload, lambda: self.fail("POST"), identity_verified=True)
        self.assertTrue(replay["replayed"])
        item = self.store.history(self.scope)["items"][0]
        self.assertEqual((item["id"], item["author"]["uid"], item["operation_ids"]), ("701", "101", ["old"]))
        self.assertNotIn("text", item)
        with database(self.store.path) as connection:
            self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM operations ORDER BY uid,operation_id")], sorted(rows, key=lambda row: row[:2]))
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        with database(self.store.path, write=True) as connection:
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 10000)

    def test_reservation_and_context_rollback_before_network(self):
        import operations
        original = operations.bind_context
        def fail_after_binding(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("fixture disk failure")
        with patch("operations.bind_context", fail_after_binding), self.assertRaises(OSError):
            self.send()
        self.assertIsNone(self.journal.read("101", "incoming-602"))
        with database(self.store.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM interaction_write_context").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM interaction_sync_state").fetchone()[0], 0)

    def test_returned_receipt_survives_failed_projection_and_restart(self):
        with patch("operations.project_receipts", side_effect=RuntimeError("fixture failure")):
            result = self.send()
        self.assertEqual(result["status"], "returned")
        self.assertFalse(result["interaction_projection_complete"])
        self.assertEqual(Journal(self.root).read("101", "incoming-602")["status"], "returned")
        restarted = InteractionHistory(self.root)
        self.assertTrue(restarted.status(self.scope)["projection_complete"])
        items = restarted.history(self.scope)["items"]
        self.assertEqual([(row["id"], row["interaction_type"], row["reply_to_id"], row["operation_ids"]) for row in items],
                         [("701", "reply", "602", ["incoming-602"])])
        self.assertTrue(self.send()["replayed"])
        self.assertEqual(restarted.status(self.scope)["my_known_comment_count"], 1)
        self.assertIsNone(items[0]["created_at"])
        self.assertNotIn("text", items[0])
        self.assertNotIn("media", items[0])

    def test_context_is_committed_before_send_and_network_does_not_hold_write_lock(self):
        payload = {"link_id": "501", "text": "fixture"}
        def send():
            with database(self.store.path, write=True) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM interaction_write_context").fetchone()[0], 1)
            return {"comment_id": "701"}
        self.journal.execute("101", "action", "comment", payload, send, context={"page": 77, "sort": "hot"})
        with database(self.store.path) as connection:
            context = connection.execute("SELECT request_page,request_sort,request_fingerprint,submission_fingerprint FROM interaction_write_context").fetchone()
            self.assertEqual((context["request_page"], context["request_sort"]), (77, "hot"))
            expected = hashlib.sha256(json_text({"link_id": "501", "root_id": None, "reply_id": None, "text": "fixture"}).encode()).hexdigest()
            self.assertEqual(context["request_fingerprint"], expected)
            self.assertIsNone(context["submission_fingerprint"])

    def test_concurrent_uid_operation_reservation_and_exact_projection(self):
        calls = []
        def run(uid):
            try:
                return self.journal.execute(uid, "same", "comment", {"link_id": "501", "text": "fixture"},
                    lambda: calls.append(uid) or {"comment_id": "701"}, identity_verified=True)
            except OperationError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(run, ["101", "102"] * 4))
        self.assertCountEqual(calls, ["101", "102"])
        for uid in ("101", "102"):
            self.assertEqual(self.store.status(Scope(uid, "501"))["my_known_comment_count"], 1)

    def test_repeated_overlapping_pages_and_exact_distinct_rounds(self):
        peers = [node("601"), node("602", parent="601", to="101"), node("603", parent="601", to="101"), node("604", uid="303")]
        self.capture(peers)
        self.send()
        self.send("second-reply", "702")
        self.send("third-reply", "703")
        replies = [node("701", uid="101", parent="602"), node("702", uid="101", parent="602"), node("704", uid="101", parent="603")]
        for _ in range(3):
            self.capture(peers + replies)
        status = self.store.status(self.scope)
        self.assertEqual(status["completed_round_count"], 2)
        self.assertEqual(status["my_known_comment_count"], 4)
        self.assertEqual(status["my_publicly_observed_comment_count"], 3)
        self.assertEqual(self.store.status(Scope("101", "501", "202", "601"))["completed_round_count"], 2)
        self.assertEqual(self.store.status(Scope("101", "501", "303"))["completed_round_count"], 0)
        self.assertEqual(self.store.status(Scope("101", "501", root_id="999"))["my_known_comment_count"], 0)
        self.assertEqual({row["incoming_comment_id"] for row in status["pending"]}, {"602", "603"})
        self.assertFalse(status["history_complete"])

    def test_pending_states_and_separate_unresolved_operation_even_when_answered(self):
        self.capture([node("602", to="101"), node("603", to="101"), node("604", to="101"), node("605", to="101"), node("606")])
        self.send()
        self.capture([node("710", uid="101", parent="603")], "own_list")
        with self.assertRaises(OperationError):
            self.journal.execute("101", "incoming-604", "reply", {"link_id": "501", "root_id": "601", "reply_id": "604", "text": "uncertain"},
                lambda: (_ for _ in ()).throw(TimeoutError()))
        status = self.store.status(self.scope)
        self.assertEqual({row["incoming_comment_id"]: row["state"] for row in status["pending"]},
                         {"602": "awaiting_public_readback", "603": "replied_visibility_unverified", "604": "write_uncertain", "605": "awaiting_my_reply"})
        self.capture([node("711", uid="101", parent="604")])
        status = self.store.status(self.scope)
        pending = next(row for row in status["pending"] if row["incoming_comment_id"] == "604")
        self.assertEqual(pending["state"], "answered")
        self.assertEqual(pending["unresolved_operation_ids"], ["incoming-604"])
        self.assertEqual(status["unresolved_operations"][0]["operation_id"], "incoming-604")

    def test_edits_sparse_observations_and_old_receipt_preserve_structural_evidence(self):
        self.send()
        self.capture([{**node("701", uid="101", parent="602", text="edited"), "updated_at": 200}], at=300)
        self.capture([{**node("701", uid=None, root=None, text=None, created=None), "media": []}], at=400)
        self.capture([{**node("701", uid="101", parent="603", text="old public"), "updated_at": 100}], at=500)
        with database(self.store.path, write=True) as connection:
            connection.execute("UPDATE interaction_write_context SET projected_at=NULL")
        self.store.recover("101")
        item = self.store.history(self.scope)["items"][0]
        self.assertEqual(item["reply_to_id"], "602")
        self.assertEqual(item["created_at"], 10)
        self.assertEqual(item["field_evidence"]["reply_to_id"], {"source": "public", "observed_at": 300, "platform_updated_at": 200})
        self.assertNotIn("text", item["field_evidence"])
        self.assertNotIn("text", item)
        self.capture([{**node("701", text="", uid="101"), "updated_at": 600}], at=600)
        self.assertEqual(self.store.history(self.scope)["items"][0]["reply_to_id"], "602")

    def test_old_snapshot_observation_cannot_overwrite_newer_public_or_creation(self):
        self.capture([node("602", parent="601", text="new", created=5)], at=300)
        self.capture([node("602", uid="303", root="609", parent="609", text="old", created=10)], at=200)
        item = self.store.history(self.scope)["items"][0]
        self.assertEqual((item["author"]["uid"], item["root_id"], item["reply_to_id"], item["created_at"], item["public_seen_at"]),
                         ("202", "601", "601", 5, 300))

    def test_immutable_post_membership_and_private_source_permissions(self):
        self.capture([node("602")])
        with self.assertRaises(XhhConfigError):
            self.capture([node("602", post="999")])
        for kind in ("own_list", "inbox"):
            with self.assertRaises(XhhConfigError):
                self.store.capture("102", [node("602")], kind, evidence(verified=False))
        with self.assertRaises(XhhConfigError):
            self.store.capture("102", [node("602", uid="102")], "receipt", evidence())
        self.assertFalse(self.store.capture("102", [node("602")], "public", evidence(verified=False))["persisted"])
        self.assertEqual(self.store.status(Scope("102", "501"))["participation"], "unknown")
        self.assertTrue(self.store.capture("101", [node("603")], "public", evidence(verified=False))["persisted"])

    def test_peer_and_root_history_resume_is_bound_and_returns_metadata_only(self):
        self.capture([node("602", text="  raw body\n" * 1000), node("603"), node("604", uid="303")])
        page = self.store.history(Scope("101", "501", "202", "601"), limit=1)
        self.assertEqual(page["items"][0]["id"], "603")
        token = page["coverage"]["resume"]
        next_page = self.store.history(Scope("101", "501", "202", "601"), limit=1, resume=token)
        self.assertEqual(next_page["items"][0]["id"], "602")
        self.assertEqual(next_page["items"][0]["author"]["uid"], "202")
        self.assertFalse({"text", "raw_text", "media", "raw"} & next_page["items"][0].keys())
        for scope in (Scope("102", "501", "202", "601"), Scope("101", "502", "202", "601"), Scope("101", "501", "303", "601"), Scope("101", "501", "202", "999")):
            with self.assertRaises(XhhConfigError):
                self.store.history(scope, resume=token)

    def test_sparse_public_cid_alone_does_not_confirm_author_and_reply_relation(self):
        self.capture([node("602", to="101")])
        self.send()
        self.capture([node("701", uid=None, parent=None, text=None, created=None)])
        status = self.store.status(self.scope)
        self.assertEqual(status["completed_round_count"], 0)
        self.assertEqual(status["my_publicly_observed_comment_count"], 0)
        self.assertEqual(status["pending"][0]["state"], "awaiting_public_readback")
        self.capture([node("701", uid="101", parent="602")])
        self.assertEqual(self.store.status(self.scope)["completed_round_count"], 1)

    def test_ambiguous_peer_and_root_relationships_cannot_inflate_filtered_rounds(self):
        self.capture([node("602", uid="303", root="800"), node("701", uid="101", root="601", parent="602", to="202")])
        self.assertEqual(self.store.status(Scope("101", "501", "202"))["completed_round_count"], 0)
        self.assertEqual(self.store.status(Scope("101", "501", root_id="601"))["completed_round_count"], 0)

    def test_newer_database_version_is_not_overwritten_or_reported_as_empty_history(self):
        self.capture([node("602")])
        with database(self.store.path, write=True) as connection:
            connection.execute("PRAGMA user_version=99")
        with self.assertRaises(XhhConfigError):
            self.store.status(self.scope)
        with self.assertRaises(XhhConfigError):
            self.send()
        with self.assertRaises(XhhConfigError):
            self.journal.read("101", "unknown")
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM interaction_comments").fetchone()[0], 1)

    def test_exact_own_list_reply_with_missing_root_is_not_treated_as_unsent_in_root_scope(self):
        self.capture([node("601", uid="101"), node("602", to="101", parent="601")])
        self.capture([node("701", uid="101", parent="602", root=None)], "own_list")
        status = self.store.status(Scope("101", "501", root_id="601"))
        self.assertEqual(status["pending"][0]["state"], "replied_visibility_unverified")
        self.assertIn("701", status["unknown_parent_ids"])
        self.assertFalse(status["ancestry_complete"])

    def test_identity_check_does_not_claim_history_coverage_and_cycles_remain_incomplete(self):
        self.store.establish("101", {"source": "fixture identity"})
        self.assertIsNone(self.store.status(self.scope)["coverage"]["last_checked_at"])
        self.capture([node("601", uid="101"), node("602", parent="603"), node("603", parent="602")])
        status = self.store.status(self.scope)
        self.assertFalse(status["ancestry_complete"])
        self.assertEqual(status["cyclic_reply_ids"], ["602", "603"])

    def test_feed_summaries_work_without_access_to_full_bodies_or_write_payloads(self):
        self.capture([node("601", text="large raw body\n" * 10000), node("701", uid="101", parent="601")])
        @contextmanager
        def bounded_database(*args, **kwargs):
            with database(*args, **kwargs) as connection:
                def authorize(action, table, column, db, trigger):
                    if action == sqlite3.SQLITE_READ and column in ("text", "media_json", "source_json", "payload_json", "raw_json"):
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                connection.set_authorizer(authorize)
                yield connection
        with patch("interaction_history.database", bounded_database):
            summaries = self.store.summaries("101", ["501", "999"])
        self.assertEqual(summaries["501"]["my_known_comment_count"], 1)
        self.assertEqual(summaries["501"]["completed_round_count"], 1)
        self.assertEqual(summaries["999"]["participation"], "unknown")


if __name__ == "__main__":
    unittest.main()
