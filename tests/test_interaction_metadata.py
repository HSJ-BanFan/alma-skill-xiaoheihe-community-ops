from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(os.environ.get("XHH_TEST_SKILL_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT / "scripts"), str(Path(__file__).resolve().parent)]

from interaction_history import InteractionHistory, Scope, database
from interaction_reads import capture_private, initial_state
from operations import Journal, OperationError
from paging import decode_resume, encode_resume
from test_interaction_cli import InteractionFixture
from test_tools import Response


CONTENT_KEYS = {"text", "raw_text", "media", "raw", "raw_json", "payload_json", "submitted_request", "request_context",
                "image_url", "image_contents", "imgs", "images", "comment_images", "avatar_url", "events", "body"}
CLI_MARKERS = ("CLI_SEND_BODY_28d154a9", "CLI_INBOX_BODY_349efc06", "CLI_OWN_BODY_9a036c11", "CLI_PUBLIC_BODY_e7194210",
               "CLI_IMAGE_29176ec3", "CLI_RAW_850c93d4")
CLI_IMAGE = "https://imgheybox.max-c.com/" + CLI_MARKERS[4] + ".png"
LEGACY_SCHEMA = (
    """CREATE TABLE operations(uid TEXT NOT NULL,operation_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
        kind TEXT NOT NULL,status TEXT NOT NULL,receipt TEXT,updated_at REAL NOT NULL,PRIMARY KEY(uid,operation_id))""",
    """CREATE TABLE interaction_write_context(uid TEXT NOT NULL,operation_id TEXT NOT NULL,post_id TEXT NOT NULL,
        root_id TEXT,reply_to_id TEXT,payload_json TEXT NOT NULL,projected_at REAL,PRIMARY KEY(uid,operation_id),
        FOREIGN KEY(uid,operation_id) REFERENCES operations(uid,operation_id))""",
    "CREATE INDEX interaction_write_target ON interaction_write_context(uid,post_id,reply_to_id)",
    """CREATE TABLE interaction_comments(uid TEXT NOT NULL,comment_id TEXT NOT NULL,post_id TEXT NOT NULL,
        root_id TEXT,author_uid TEXT,reply_to_id TEXT,reply_to_uid TEXT,text TEXT,media_json TEXT,platform_created_at REAL,
        first_seen_at REAL NOT NULL,last_seen_at REAL NOT NULL,public_seen_at REAL,own_list_seen_at REAL,receipt_seen_at REAL,
        source_json TEXT NOT NULL,field_evidence_json TEXT NOT NULL,PRIMARY KEY(uid,comment_id))""",
    "CREATE INDEX interaction_post_author ON interaction_comments(uid,post_id,author_uid,platform_created_at,comment_id)",
    "CREATE INDEX interaction_thread_author ON interaction_comments(uid,post_id,root_id,author_uid)",
    "CREATE INDEX interaction_reply_target ON interaction_comments(uid,post_id,reply_to_id)",
    """CREATE TABLE interaction_inbox(uid TEXT NOT NULL,event_key TEXT NOT NULL,post_id TEXT NOT NULL,
        incoming_comment_id TEXT NOT NULL,root_id TEXT,peer_uid TEXT,first_seen_at REAL NOT NULL,last_seen_at REAL NOT NULL,
        raw_json TEXT NOT NULL,PRIMARY KEY(uid,event_key))""",
    "CREATE INDEX interaction_inbox_comment ON interaction_inbox(uid,post_id,incoming_comment_id)",
    """CREATE TABLE interaction_sync_state(uid TEXT NOT NULL,query_key TEXT NOT NULL,post_id TEXT,root_id TEXT,peer_uid TEXT,
        source TEXT NOT NULL,last_checked_at REAL NOT NULL,coverage_json TEXT NOT NULL,PRIMARY KEY(uid,query_key))""",
)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class MetadataFixture(InteractionFixture):
    def __init__(self):
        super().__init__()
        self.public_own = True
        self.incoming.update(comment_a_text=CLI_MARKERS[1], comment_b_text=CLI_MARKERS[2], imgs=[CLI_IMAGE],
                             comment_a_create_at=20, comment_b_create_at=10,
                             extra={"raw": CLI_MARKERS[5], "images": [CLI_IMAGE]})

    def __call__(self, request, timeout=None):
        path = urlsplit(request.full_url).path
        if path not in {"/account/restore_login", "/bbs/app/link/tree", "/bbs/app/comment/create", "/bbs/app/user/message",
                        "/bbs/app/profile/bbs/comment/list", "/bbs/app/comment/sub/comments", "/bbs/app/link/like/combo",
                        "/bbs/app/link/favour"}:
            raise AssertionError("fixture received an unexpected endpoint")
        value = json.loads(super().__call__(request, timeout).read())
        value["extra"] = {"body": CLI_MARKERS[5], "media": [CLI_IMAGE]}
        if path == "/bbs/app/link/tree":
            link = value["result"]["link"]
            link.update(text=CLI_MARKERS[3] + '<img src="' + CLI_IMAGE + '">', imgs=[CLI_IMAGE])
            for floor in value["result"]["comments"]:
                for node in floor["comment"]:
                    node.update(text=CLI_MARKERS[3], comment_images=[CLI_IMAGE], raw={"body": CLI_MARKERS[5]},
                                create_at={"601": 10, "602": 20, "701": 50}.get(node["commentid"], 5))
        elif path == "/bbs/app/profile/bbs/comment/list":
            for node in value["result"]:
                node.update(text=CLI_MARKERS[2], comment_images=[CLI_IMAGE], raw={"body": CLI_MARKERS[5]})
        elif path == "/bbs/app/comment/sub/comments":
            for node in value["result"]["comments"]:
                node.update(text=CLI_MARKERS[3], imgs=[CLI_IMAGE], raw={"body": CLI_MARKERS[5]})
        elif path == "/bbs/app/comment/create":
            value.update(text=CLI_MARKERS[0], imgs=[CLI_IMAGE])
        return Response(value)


class InteractionMetadataTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xhh-metadata-")
        self.folder = Path(self.temp.name)
        self.root = self.folder / "state"
        self.store = InteractionHistory(self.root)
        self.journal = Journal(self.root)
        self.scope = Scope("101", "501")
        self.client = SimpleNamespace(config=SimpleNamespace(heybox_id="101", protocol_mode="web"))
        self.body = "DISTINCT_PRIVATE_BODY_e68e6b71"
        self.raw = "DISTINCT_EXTRA_RAW_f2748a19"
        self.image = "https://imgheybox.max-c.com/DISTINCT_MEDIA_e6d59306.png"
        self.markers = (self.body, self.raw, self.image)

    def tearDown(self):
        self.temp.cleanup()

    def proof(self, route="/bbs/app/link/tree"):
        return {"route": route, "protocol": "web", "configured_uid": "101", "observed_at": 100,
                "identity_verified": True, "identity_verified_this_read": True,
                "text": self.body, "raw": {"body": self.raw, "images": [self.image]}, "events": [{"text": self.raw}]}

    def node(self, cid="602", uid="202", parent="601"):
        return {"id": cid, "post_id": "501", "root_id": "601", "author": {"uid": uid, "name": self.raw, "avatar_url": self.image},
                "reply_to_id": parent, "reply_to_uid": "101", "created_at": 20, "updated_at": 30,
                "text": self.body, "raw_text": self.body, "media": [{"url": self.image, "alt": self.raw}],
                "raw": {"body": self.raw}, "source": self.proof(), "events": [{"text": self.raw}]}

    def assert_no_content(self, value, markers=None):
        for marker in markers or self.markers:
            self.assertNotIn(marker, encoded(value))
        if isinstance(value, dict):
            self.assertFalse(CONTENT_KEYS & value.keys(), value.keys())
            for item in value.values():
                self.assert_no_content(item, markers)
        elif isinstance(value, list):
            for item in value:
                self.assert_no_content(item, markers)

    def assert_private_storage(self, markers=None, *, inspect_bytes=True):
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {"operations", "interaction_write_context", "interaction_comments", "interaction_inbox", "interaction_sync_state"})
            columns = {table: {row[1] for row in connection.execute(f"PRAGMA table_info({table})")} for table in tables}
            self.assertEqual(columns["interaction_comments"], {"uid", "comment_id", "post_id", "root_id", "author_uid", "reply_to_id",
                "reply_to_uid", "platform_created_at", "first_seen_at", "last_seen_at", "public_seen_at", "own_list_seen_at",
                "receipt_seen_at", "source_json", "field_evidence_json"})
            self.assertEqual(columns["interaction_write_context"], {"uid", "operation_id", "post_id", "root_id", "reply_to_id",
                "request_fingerprint", "submission_fingerprint", "request_protocol", "request_page", "request_limit", "request_sort",
                "request_cursor", "projected_at"})
            self.assertEqual(columns["interaction_inbox"], {"uid", "event_key", "post_id", "incoming_comment_id", "root_id", "peer_uid",
                "first_seen_at", "last_seen_at"})
            for table in tables:
                self.assertFalse(CONTENT_KEYS & columns[table])
                for row in connection.execute(f"SELECT * FROM {table}"):
                    for cell in row:
                        if isinstance(cell, str):
                            for marker in markers or self.markers:
                                self.assertNotIn(marker, cell)
                            if cell.startswith(("{", "[")):
                                self.assert_no_content(json.loads(cell), markers)
        self.assert_no_content(self.store.history(self.scope), markers)
        if inspect_bytes:
            for suffix in ("", "-wal", "-shm"):
                path = Path(str(self.store.path) + suffix)
                if path.exists():
                    for marker in markers or self.markers:
                        self.assertNotIn(marker.encode(), path.read_bytes())

    def test_public_observations_keep_only_structural_fields_and_known_proof(self):
        state = {**initial_state(), "phase": "comments", "raw": {"text": self.raw}, "media": [self.image]}
        proof = self.proof()
        self.store.capture("101", [self.node("601", "101", None), self.node(), self.node("701", "101", "602")], "public", proof,
            scope=self.scope, coverage={"position": 1, "next": 2, "mode": "page", "completion": "more", "raw": self.raw,
                "body": {"text": self.body}, "media": [self.image], "continuation": state,
                "budget": {"used_requests": 1, "request_limit": 3, "seconds_remaining": 20, "raw": self.raw},
                "error": {"type": "XhhConfigError", "code": self.raw, "status": self.body, "raw": {"text": self.raw}}})
        history = self.store.history(self.scope)
        own = next(item for item in history["items"] if item["id"] == "701")
        self.assertEqual((own["interaction_type"], own["author"]["uid"], own["reply_to_id"], own["created_at"]), ("reply", "101", "602", 20))
        self.assertEqual(history["status"]["completed_round_count"], 1)
        self.assertEqual(own["sources"]["public:/bbs/app/link/tree"]["configured_uid"], "101")
        self.assertEqual(history["status"]["coverage"]["sources"][0]["continuation"], {key: state[key] for key in initial_state()})
        self.assertEqual(history["status"]["coverage"]["sources"][0]["error"], {"type": "XhhConfigError"})
        self.assert_private_storage()

    def test_inbox_bridge_drops_bodies_images_and_raw_events_without_changing_live_data(self):
        data = {"entries": [{"message_id": {"body": self.raw, "images": [self.image]}, "comment_a_id": "602", "comment_b_id": "601",
            "root_comment_id": "601", "linkid": "501", "comment_a_userid": "202", "comment_b_userid": "101",
            "comment_a_text": self.body, "comment_b_text": self.raw, "comment_a_create_at": 20, "comment_b_create_at": 10,
            "images": [self.image], "extra": {"raw": self.raw}}], "has_more": False}
        before = encoded(data)
        capture_private(self.store, self.client, "inbox", data, {"offset": 0, "limit": 20, "next_offset": 1, "raw": self.raw})
        self.assertEqual(encoded(data), before)
        history = self.store.history(self.scope)
        self.assertEqual({item["id"] for item in history["items"]}, {"601", "602"})
        self.assertEqual(history["status"]["pending"][0]["incoming_comment_id"], "602")
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute("SELECT event_key,peer_uid FROM interaction_inbox").fetchone(), ("incoming:602", "202"))
        self.assert_private_storage()

    def test_inbox_imports_known_parent_and_peer_without_any_body_fields(self):
        data = {"entries": [{"message_id": "structural-only", "comment_a_id": "602", "comment_b_id": "601", "root_comment_id": "601",
                             "linkid": "501", "comment_a_userid": "202", "comment_b_userid": "101"}]}
        capture_private(self.store, self.client, "inbox", data, {"offset": 0, "limit": 20, "next_offset": 1})
        history = self.store.history(Scope("101", "501", "202", "601"))
        items = {item["id"]: item for item in history["items"]}
        self.assertEqual(set(items), {"601", "602"})
        self.assertEqual(items["601"]["author"]["uid"], "101")
        self.assertEqual((items["602"]["author"]["uid"], items["602"]["reply_to_id"], items["602"]["reply_to_uid"]), ("202", "601", "101"))
        self.assertEqual(history["status"]["pending"][0]["state"], "awaiting_my_reply")
        self.assert_private_storage()

    def test_own_list_keeps_peer_and_parent_facts_without_retaining_live_content(self):
        data = [{"comment": {"commentid": "701", "linkid": "501", "root_comment_id": "601", "replyid": "602", "replyuserid": "202",
                 "userid": "101", "text": self.body, "comment_images": [self.image], "create_at": 30, "raw": {"body": self.raw}},
                 "link": {"id": "501", "text": self.raw, "imgs": [self.image]}}]
        before = encoded(data)
        capture_private(self.store, self.client, "my-comments", data, {"offset": 0, "limit": 20, "next_offset": 1})
        self.assertEqual(encoded(data), before)
        item = self.store.history(self.scope)["items"][0]
        self.assertEqual((item["id"], item["reply_to_id"], item["reply_to_uid"], item["author"]["uid"]), ("701", "602", "202", "101"))
        self.assertEqual(self.store.status(self.scope)["participation"], "confirmed")
        self.assertEqual(self.store.status(self.scope)["completed_round_count"], 0)
        self.assert_private_storage()

    def test_conflicting_receipt_target_remains_returned_without_projecting_a_comment(self):
        payload = {"link_id": "501", "root_id": "601", "reply_id": "602", "text": self.body}
        receipt = {"comment_id": "701", "link_id": "999", "text": self.body, "raw": {"images": [self.image], "body": self.raw}}
        returned = self.journal.execute("101", "conflicting-receipt", "reply", payload, lambda: receipt, identity_verified=True)
        self.assertEqual(returned["status"], "returned")
        self.assertFalse(returned["interaction_projection_complete"])
        self.assertEqual(self.journal.read("101", "conflicting-receipt")["receipt"], {"comment_id": "701", "link_id": "999"})
        history = self.store.history(self.scope)
        self.assertFalse(history["status"]["projection_complete"])
        self.assertEqual(history["items"], [])
        self.assertEqual(history["status"]["pending"][0]["state"], "awaiting_public_readback")
        self.assertTrue(self.journal.execute("101", "conflicting-receipt", "reply", payload, lambda: self.fail("conflicting receipt resent"))["replayed"])
        self.assert_private_storage()

    def test_history_pagination_keeps_its_position_when_newer_records_arrive(self):
        nodes = [{**self.node("601", "101", None), "created_at": 10}, {**self.node(), "created_at": 20},
                 {**self.node("701", "101", "602"), "created_at": 30}]
        self.store.capture("101", nodes, "public", self.proof())
        scope = Scope("101", "501", "202", "601")
        first = self.store.history(scope, limit=1)
        self.assertEqual(first["items"][0]["id"], "701")
        self.store.capture("101", [{**self.node("801"), "created_at": 40}, nodes[0]], "public", {**self.proof(), "observed_at": 200})
        rest = self.store.history(scope, limit=2, resume=first["coverage"]["resume"])
        self.assertEqual([item["id"] for item in rest["items"]], ["602", "601"])
        self.assertFalse(rest["coverage"]["has_more_local"])
        self.assertEqual(rest["status"]["completed_round_count"], 1)
        self.assertEqual(self.store.history(scope, limit=1)["items"][0]["id"], "801")
        self.assert_private_storage()

    def test_submission_hash_and_exact_durable_receipt_preserve_content_sensitive_replay(self):
        payload = {"link_id": "501", "root_id": "601", "reply_id": "602", "text": self.body, "image_url": self.image, "raw": self.raw}
        request = {key: payload[key] for key in ("link_id", "root_id", "reply_id", "text")}
        request.update(imgs=self.image, is_cy="0", extra={"text": self.raw})
        provenance = {"protocol": "web", "page": 77, "limit": 20, "sort": "hot", "cursor": "cursor-one",
                      "text": self.body, "images": [self.image], "raw": {"body": self.raw}}
        receipt = {"comment_id": "701", "link_id": "501", "root_id": "601", "reply_id": "602", "visibility": "not_checked",
                   "text": self.body, "media": [self.image], "raw": {"text": self.raw}}
        calls = []
        def send():
            self.journal.bind_submitted_request("101", "exact-send", request)
            with database(self.store.path, write=True) as connection:
                connection.execute("BEGIN IMMEDIATE")
                context = connection.execute("SELECT * FROM interaction_write_context").fetchone()
                self.assertEqual(context["submission_fingerprint"], digest(request))
            calls.append(1)
            return receipt
        returned = self.journal.execute("101", "exact-send", "reply", payload, send, identity_verified=True, context=provenance)
        self.assertEqual(returned["receipt"]["raw"], {"text": self.raw})
        durable = self.journal.read("101", "exact-send")["receipt"]
        self.assertEqual(durable, {key: receipt[key] for key in ("comment_id", "link_id", "root_id", "reply_id", "visibility")})
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute("SELECT fingerprint FROM operations").fetchone()[0], digest({"kind": "reply", "payload": payload}))
            values = connection.execute("SELECT request_fingerprint,request_protocol,request_page,request_limit,request_sort,request_cursor FROM interaction_write_context").fetchone()
            self.assertEqual(values, (digest({key: payload[key] for key in ("link_id", "root_id", "reply_id", "text")}), "web", 77, 20, "hot", "cursor-one"))
        replay = self.journal.execute("101", "exact-send", "reply", payload, lambda: self.fail("resend"))
        self.assertTrue(replay["replayed"])
        self.assert_no_content(replay)
        for changed in ({**payload, "text": self.body + " changed"}, {**payload, "image_url": self.image + "?changed=1"}):
            with self.assertRaises(OperationError) as error:
                self.journal.execute("101", "exact-send", "reply", changed, lambda: self.fail("resend"))
            self.assertEqual(error.exception.code, "operation_id_conflict")
        self.assertEqual(calls, [1])
        self.assertEqual(self.store.history(self.scope)["items"][0]["operation_ids"], ["exact-send"])
        self.assert_private_storage()

    def test_before_post_rejects_each_changed_reserved_field_by_hash(self):
        payload = {"link_id": "501", "root_id": "601", "reply_id": "602", "text": self.body}
        posted = []
        for key, replacement in (("link_id", "999"), ("root_id", "999"), ("reply_id", "999"), ("text", self.body + " changed")):
            operation = "mismatch-" + key
            def send():
                self.journal.bind_submitted_request("101", operation, {**payload, key: replacement, "imgs": self.image, "is_cy": "0"})
                posted.append(operation)
                return {"comment_id": "701"}
            with self.subTest(key=key), self.assertRaises(OperationError) as error:
                self.journal.execute("101", operation, "reply", payload, send, identity_verified=True)
            self.assertEqual(error.exception.code, "write_uncertain")
            with self.assertRaises(OperationError) as replay:
                self.journal.execute("101", operation, "reply", payload, lambda: self.fail("closed reservation resent"))
            self.assertEqual(replay.exception.code, "operation_pending_or_uncertain")
        self.assertEqual(posted, [])
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM interaction_write_context WHERE submission_fingerprint IS NOT NULL").fetchone()[0], 0)
        self.assert_private_storage()

    def test_post_actions_never_project_comment_cids_or_count_discussion_rounds(self):
        for kind in ("like", "favourite", "unfavourite"):
            receipt = {"link_id": "501", "comment_id": "999", "text": self.body, "images": [self.image], "raw": self.raw}
            returned = self.journal.execute("101", kind + "-one", kind, {"link_id": "501"}, lambda: receipt, identity_verified=True)
            self.assertTrue(returned["interaction_projection_complete"])
        status = self.store.status(self.scope)
        self.assertEqual({action["interaction_type"] for action in status["known_actions"]}, {"like", "favourite", "unfavourite"})
        self.assertEqual((status["participation"], status["my_known_comment_count"], status["completed_round_count"]), ("unknown", 0, 0))
        self.assertEqual(status["unresolved_operations"], [])
        self.assertEqual(self.store.history(self.scope)["items"], [])
        self.assertEqual(self.store.status(Scope("101", "501", "202"))["known_actions"], [])
        self.assertEqual(self.store.status(Scope("101", "501", root_id="601"))["known_actions"], [])
        self.assertEqual(self.store.status(Scope("102", "501"))["known_actions"], [])
        self.assertEqual(self.store.summaries("101", ["501"])["501"]["my_known_comment_count"], 0)
        self.assertTrue(self.journal.execute("101", "like-one", "like", {"link_id": "501"}, lambda: self.fail("action resend"))["replayed"])
        self.assert_private_storage()

    def test_uncertain_action_is_labelled_separately_from_uncertain_comment(self):
        with self.assertRaises(OperationError):
            self.journal.execute("101", "uncertain-like", "like", {"link_id": "501"},
                                 lambda: (_ for _ in ()).throw(TimeoutError()), identity_verified=True)
        status = self.store.status(self.scope)
        self.assertEqual(status["known_actions"][0]["status"], "uncertain")
        self.assertEqual(status["known_actions"][0]["source"], "operation_reservation")
        self.assertEqual((status["participation"], status["pending"], status["unresolved_operations"]), ("unknown", [], []))

    def test_upload_receipts_keep_replay_metadata_and_publish_discards_content(self):
        upload_url = "https://imgheybox.max-c.com/receipt-result.png"
        cases = (("upload", {"image_sha256": digest(self.body)}, {"url": upload_url, "width": 1, "height": 1, "raw": self.raw},
                  {"url": upload_url, "width": 1, "height": 1}),
                 ("publish", {"text": self.body, "imgs": [self.image]}, {"link_id": "801", "draft": True, "text": self.body,
                     "raw": {"media": [self.image], "body": self.raw}}, {"link_id": "801", "draft": True}))
        for kind, payload, receipt, expected in cases:
            returned = self.journal.execute("101", kind + "-receipt", kind, payload, lambda: receipt)
            self.assertEqual(returned["receipt"], receipt)
            self.assertEqual(self.journal.read("101", kind + "-receipt")["receipt"], expected)
            replay = self.journal.execute("101", kind + "-receipt", kind, payload, lambda: self.fail("media operation resent"))
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["receipt"], expected)
        self.assertEqual(self.store.history(self.scope)["known_actions"], [])
        self.assertNotIn(upload_url, encoded(self.store.history(self.scope)))
        self.assert_private_storage()

    def legacy(self):
        self.root.mkdir()
        payloads = {
            "v1-comment": {"link_id": "501", "text": self.body, "image_url": self.image},
            "v1-reply": {"link_id": "501", "root_id": "601", "reply_id": "602", "text": self.body},
            "v1-recover": {"link_id": "501", "text": self.body},
            "v1-uncertain": {"link_id": "501", "root_id": "601", "reply_id": "603", "text": self.body},
            "v1-reserved": {"link_id": "501", "root_id": "601", "reply_id": "604", "text": self.body},
            "v1-other": {"link_id": "999", "text": self.body},
        }
        submitted = {**payloads["v1-reply"], "imgs": self.image, "is_cy": "0", "extra": {"raw": self.raw}}
        operations = []
        timestamps = {}
        continuation = {**initial_state(), "phase": "replies", "page": 2, "roots": ["601"], "cursor": "next-v1",
                        "inbox_offset": 1, "own_list_offset": 1}
        binding = {**self.scope.binding(), "protocol": "web", "limit": 20, "sort": "time_aes"}
        token = encode_resume("interaction-sync", "101", {"binding": {**binding, "raw": self.raw},
            "state": {**continuation, "text": self.body, "media": [self.image]}, "events": [{"raw": self.raw}]})
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA secure_delete=OFF")
            for sql in LEGACY_SCHEMA:
                connection.execute(sql)
            specs = [("101", "v1-comment", "comment", "returned", "701", 65), ("101", "v1-reply", "reply", "returned", "702", 85),
                     ("101", "v1-recover", "comment", "returned", "703", None), ("101", "v1-uncertain", "reply", "uncertain", None, None),
                     ("101", "v1-reserved", "reply", "reserved", None, None), ("102", "v1-other", "comment", "returned", "801", 95)]
            for index, (uid, operation, kind, status, cid, projected) in enumerate(specs):
                payload = payloads[operation]
                receipt = {"comment_id": cid, "link_id": payload["link_id"], "text": self.body, "raw": {"images": [self.image], "body": self.raw}} if cid else None
                row = (uid, operation, digest({"kind": kind, "payload": payload}), kind, status, encoded(receipt) if receipt else None, 70 + index)
                operations.append(row)
                connection.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?)", row)
                context = {**payload, "request_context": {"protocol": "web", "page": 3, "limit": 20, "sort": "time_aes", "cursor": "next-v1",
                           "text": self.body, "raw": {"media": [self.image]}}, "extra": {"raw": self.raw}}
                if operation == "v1-reply":
                    context["submitted_request"] = submitted
                connection.execute("INSERT INTO interaction_write_context VALUES (?,?,?,?,?,?,?)",
                    (uid, operation, payload["link_id"], payload.get("root_id"), payload.get("reply_id"), encoded(context), projected))
            for uid, cid, post, root, author, parent, peer, created, own, receipt_at in (
                    ("101", "601", "501", "601", "101", None, None, 10, None, None),
                    ("101", "602", "501", "601", "202", "601", "101", 20, None, None),
                    ("101", "701", "501", "701", "101", None, None, 25, None, 70),
                    ("101", "702", "501", "601", "101", "602", "202", 30, 80, 71),
                    ("102", "801", "999", "801", "102", None, None, 15, 80, 75)):
                proof = {**self.proof(), "configured_uid": uid, "kind": "public", "first_observed_at": 50, "last_observed_at": 100,
                         "raw": {"events": [{"text": self.raw, "images": [self.image]}]}}
                fields = {key: {"source": "public", "observed_at": 100, "platform_updated_at": 40, "raw": {"text": self.raw}}
                          for key in ("root_id", "author_uid", "reply_to_id", "reply_to_uid", "platform_created_at", "text", "media_json")}
                connection.execute("INSERT INTO interaction_comments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uid, cid, post, root, author, parent, peer, self.body, encoded([{"url": self.image}]), created, 50, 100, 100, own, receipt_at,
                     encoded({"public:/bbs/app/link/tree": proof, "raw": {"text": self.raw}}), encoded(fields)))
                timestamps[cid] = (created, 50, 100, 100, own, receipt_at)
            connection.execute("INSERT INTO interaction_inbox VALUES (?,?,?,?,?,?,?,?,?)",
                ("101", "message:old-notice", "501", "602", "601", "202", 33, 44, encoded({"text": self.body, "images": [self.image], "raw": self.raw})))
            identity = {"account_uid": "101", "identity_established_at": 11, "evidence": {"source": "sync_identity_check", "protocol": "web",
                        "text": self.body, "raw": {"media": [self.image]}}, "raw": self.raw}
            connection.execute("INSERT INTO interaction_sync_state VALUES (?,?,NULL,NULL,NULL,?,?,?)", ("101", "identity", "verified_identity", 77, encoded(identity)))
            coverage = {"scope": "bounded_sources", "completion": "unknown", "history_complete": False, "stop_reason": "request_budget",
                        "pages_read": 2, "gaps": ["inbox_endpoint_unknown", self.raw], "resume": token,
                        "continuation": {**continuation, "text": self.body, "media": [self.image]},
                        "budget": {"used_requests": 3, "request_limit": 10, "seconds_remaining": 8, "raw": self.raw},
                        "raw": {"body": self.raw}, "events": [{"images": [self.image]}]}
            connection.execute("INSERT INTO interaction_sync_state VALUES (?,?,?,NULL,NULL,?,?,?)", ("101", "sync-check", "501", "sync", 88, encoded(coverage)))
            connection.execute("PRAGMA user_version=1")
        self.assertIn(self.body.encode(), self.store.path.read_bytes())
        return operations, payloads, submitted, timestamps, continuation, binding

    def test_first_local_v1_access_migrates_content_while_preserving_facts_and_recovery(self):
        operations, payloads, submitted, timestamps, continuation, binding = self.legacy()
        history = self.store.history(self.scope)
        self.assertTrue(history["status"]["projection_complete"])
        self.assertEqual({item["id"] for item in history["items"]}, {"601", "602", "701", "702", "703"})
        self.assertEqual(history["status"]["completed_round_count"], 1)
        with closing(sqlite3.connect(self.store.path)) as connection:
            current = list(connection.execute("SELECT uid,operation_id,fingerprint,kind,status,updated_at FROM operations ORDER BY uid,operation_id"))
            self.assertEqual(current, sorted([row[:5] + (row[6],) for row in operations]))
            for cid, expected in timestamps.items():
                self.assertEqual(connection.execute("SELECT platform_created_at,first_seen_at,last_seen_at,public_seen_at,own_list_seen_at,receipt_seen_at FROM interaction_comments WHERE comment_id=?", (cid,)).fetchone(), expected)
            self.assertEqual(connection.execute("SELECT root_id,reply_to_id,submission_fingerprint,request_page,request_cursor,projected_at FROM interaction_write_context WHERE operation_id='v1-reply'").fetchone(),
                             ("601", "602", digest(submitted), 3, "next-v1", 85))
            self.assertEqual(connection.execute("SELECT request_fingerprint FROM interaction_write_context WHERE operation_id='v1-reply'").fetchone()[0], digest(payloads["v1-reply"]))
            self.assertEqual(connection.execute("SELECT event_key,incoming_comment_id,peer_uid,first_seen_at,last_seen_at FROM interaction_inbox").fetchone(), ("message:old-notice", "602", "202", 33, 44))
            identity = json.loads(connection.execute("SELECT coverage_json FROM interaction_sync_state WHERE query_key='identity'").fetchone()[0])
            self.assertEqual(identity["identity_established_at"], 11)
            scope_coverage = json.loads(connection.execute("SELECT coverage_json FROM interaction_sync_state WHERE query_key='sync-check'").fetchone()[0])
            self.assertEqual(scope_coverage["continuation"], continuation)
            self.assertEqual(decode_resume(scope_coverage["resume"], "interaction-sync", "101"), {"binding": binding, "state": continuation})
            self.assertEqual(connection.execute("SELECT last_checked_at FROM interaction_sync_state WHERE query_key='sync-check'").fetchone()[0], 88)
        own = next(item for item in history["items"] if item["id"] == "702")
        self.assertEqual(own["sources"]["public:/bbs/app/link/tree"]["first_observed_at"], 50)
        self.assertEqual(own["field_evidence"]["reply_to_id"], {"source": "public", "observed_at": 100, "platform_updated_at": 40})
        self.assertEqual({item["id"] for item in self.store.history(Scope("102", "999"))["items"]}, {"801"})
        self.assertTrue(self.journal.execute("101", "v1-reply", "reply", payloads["v1-reply"], lambda: self.fail("migration resent"))["replayed"])
        with self.assertRaises(OperationError) as error:
            self.journal.execute("101", "v1-reply", "reply", {**payloads["v1-reply"], "text": "changed"}, lambda: self.fail("migration conflict resent"))
        self.assertEqual(error.exception.code, "operation_id_conflict")
        self.assert_private_storage()

    def test_v1_migration_failure_rolls_back_all_tables_receipts_and_version(self):
        self.legacy()
        with patch("interaction_history.clean_receipts", side_effect=sqlite3.OperationalError("fixture interrupted migration")), self.assertRaises(sqlite3.OperationalError):
            self.store.history(self.scope)
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIn("text", {row[1] for row in connection.execute("PRAGMA table_info(interaction_comments)")})
            self.assertEqual(connection.execute("SELECT text FROM interaction_comments WHERE comment_id='601'").fetchone()[0], self.body)
            self.assertFalse(any(row[0].endswith("_v1") for row in connection.execute("SELECT name FROM sqlite_master")))
        self.store.history(self.scope)
        self.assert_private_storage()

    def test_old_wal_reader_can_keep_legacy_snapshot_after_metadata_migration(self):
        self.legacy()
        with closing(sqlite3.connect(self.store.path)) as reader:
            reader.execute("BEGIN")
            self.assertEqual(reader.execute("SELECT text FROM interaction_comments WHERE comment_id='601'").fetchone()[0], self.body)
            self.store.history(self.scope)
            self.assertEqual(reader.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(reader.execute("SELECT text FROM interaction_comments WHERE comment_id='601'").fetchone()[0], self.body)
            self.assert_private_storage(inspect_bytes=False)
        with database(self.store.path, write=True) as connection:
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            self.assertEqual(checkpoint[0], 0)
            self.assertEqual(connection.execute("PRAGMA secure_delete").fetchone()[0], 1)
        self.assert_private_storage()

    def test_v0_local_operation_query_does_not_upgrade_or_expose_content(self):
        self.root.mkdir()
        payload = {"link_id": "501", "text": self.body}
        receipt = {"comment_id": "701", "text": self.body, "media": [self.image], "raw": self.raw}
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute(LEGACY_SCHEMA[0])
            connection.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?)", ("101", "v0", digest({"kind": "comment", "payload": payload}), "comment", "returned", encoded(receipt), 10))
        self.assertEqual(self.journal.read("101", "v0")["receipt"], {"comment_id": "701"})
        self.assertTrue(self.store.status(self.scope)["legacy_targets_unknown"])
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual([row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")], ["operations"])
            self.assertEqual(json.loads(connection.execute("SELECT receipt FROM operations").fetchone()[0]), receipt)
        self.assertTrue(self.journal.execute("101", "v0", "comment", payload, lambda: self.fail("v0 resent"))["replayed"])
        self.assert_private_storage()

    def test_isolated_cli_write_read_sync_replay_and_offline_history_with_fixtures(self):
        skill = self.folder / "separate skill"
        shutil.copytree(ROOT / "scripts", skill / "scripts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ROOT / "tests", skill / "tests", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        runner = skill / "fixture_cli.py"
        runner.write_text('''import json
import sys
from pathlib import Path
from unittest.mock import patch
sys.path[:0] = [str(Path(__file__).parent / "scripts"), str(Path(__file__).parent / "tests")]
import xhh
from test_interaction_metadata import MetadataFixture
fixture = MetadataFixture()
with patch("urllib.request.urlopen", fixture), patch("paging.read_http", side_effect=lambda request, timeout, **kwargs: fixture(request, timeout).read()), patch("socket.create_connection", side_effect=AssertionError("real sockets forbidden")):
    code = xhh.main()
sys.stderr.write(json.dumps(fixture.calls, ensure_ascii=False))
raise SystemExit(code)
''', encoding="utf-8")
        config = self.folder / "fixture.json"
        config.write_text(encoded({"heybox_id": "101", "pkey": "fixture-secret-only", "protocol_mode": "web", "api_base": "https://127.0.0.1:1"}), encoding="utf-8")
        body = self.folder / "body.txt"
        body.write_text(CLI_MARKERS[0], encoding="utf-8")
        unrelated = self.folder / "unrelated cwd"
        unrelated.mkdir()
        environment = {key: value for key, value in os.environ.items() if not key.startswith("XHH_") and key != "PYTHONPATH"}
        def invoke(*arguments):
            result = subprocess.run([sys.executable, "-I", "-S", "-X", "utf8", str(runner), "--config", str(config), "--data-dir", str(self.root), *arguments],
                cwd=unrelated, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout), json.loads(result.stderr)
        reply = ("reply", "501", "601", "602", "--text-file", str(body), "--operation-id", "isolated-reply", "--confirm")
        first, calls = invoke(*reply)
        self.assertEqual(first["data"]["receipt"]["comment_id"], "701")
        posts = [call for call in calls if call[1] == "POST"]
        self.assertEqual(posts[0][2]["text"], [CLI_MARKERS[0]])
        replay, calls = invoke(*reply)
        self.assertTrue(replay["data"]["replayed"])
        self.assertFalse(any(call[1] == "POST" for call in calls))
        inbox, _ = invoke("inbox")
        self.assertEqual(inbox["data"]["entries"][0]["comment_a_text"], CLI_MARKERS[1])
        self.assertEqual(inbox["data"]["entries"][0]["imgs"], [CLI_IMAGE])
        own, _ = invoke("my-comments")
        self.assertEqual(own["data"][0]["text"], CLI_MARKERS[2])
        self.assertEqual(own["data"][0]["comment_images"], [CLI_IMAGE])
        post, calls = invoke("post", "501", "--requests", "1")
        self.assertEqual(len(calls), 1)
        self.assertIn(CLI_MARKERS[3], post["data"]["body"]["text"])
        self.assertIn(CLI_IMAGE, [asset["url"] for asset in post["data"]["media"]])
        comments, _ = invoke("comments", "501", "--requests", "1")
        self.assertIn(CLI_IMAGE, [asset["url"] for asset in comments["data"]["items"][0]["media"]])
        thread, _ = invoke("thread", "501", "601", "--around", "701", "--pages", "2")
        self.assertTrue(thread["data"]["target_observed"])
        self.assertIn(CLI_MARKERS[3], encoded(thread["data"]))
        for kind in ("like", "favourite", "unfavourite"):
            invoke(kind, "501", "--operation-id", "isolated-" + kind, "--confirm")
        sync, _ = invoke("interaction", "sync", "501", "--requests", "3", "--pages", "10")
        self.assertEqual(sync["coverage"]["stop_reason"], "request_budget")
        self.assertTrue(sync["coverage"]["resume"])
        history, calls = invoke("interaction", "history", "501", "--limit", "1")
        self.assertEqual(calls, [])
        self.assertEqual(history["data"]["items"][0]["id"], "701")
        self.assertEqual(history["data"]["status"]["completed_round_count"], 1)
        self.assertEqual({action["interaction_type"] for action in history["data"]["known_actions"]}, {"like", "favourite", "unfavourite"})
        rest, calls = invoke("interaction", "history", "501", "--limit", "2", "--resume", history["coverage"]["resume"])
        self.assertEqual(calls, [])
        self.assertEqual({item["id"] for item in rest["data"]["items"]}, {"601", "602"})
        self.assert_no_content(history, CLI_MARKERS)
        self.assert_no_content(rest, CLI_MARKERS)
        operation, calls = invoke("operation", "isolated-reply")
        self.assertEqual(calls, [])
        self.assertEqual(operation["data"]["receipt"]["reply_id"], "602")
        self.assert_private_storage(CLI_MARKERS)


if __name__ == "__main__":
    unittest.main()
