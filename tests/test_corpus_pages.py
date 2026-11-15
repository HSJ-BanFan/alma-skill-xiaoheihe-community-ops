import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import xhh
from browser import Browser
from corpus import CorpusBudget, CorpusReader
from corpus_model import (
    CorpusManifest,
    PageEvidence,
    PageResult,
    Position,
    ProviderItem,
    frame_key,
    sha256_value,
)
from corpus_store import CorpusConflict, CorpusLocked, CorpusStore
from observations import Store
from paging import Budget, coverage
from xhh_sdk.config import XhhConfig


MANIFEST = {
    "schema_version": 1,
    "run_id": "page-run",
    "account": {"alias": "fixture", "uid": "101"},
    "reader": {"protocol": "app", "page_size": 5},
    "limits": {"requests": 10, "seconds": 30, "unique_records": 20},
    "queries": [{
        "query_id": "feed-one",
        "kind": "feed",
        "target": {"topic_id": "7214"},
        "filters": {},
        "limits": {"pages": 3, "new_records": 5},
    }],
}


class PageClient:
    def __init__(self):
        self.config = XhhConfig(
            pkey="fixture", heybox_id="101", protocol_mode="app")
        self.transport = self
        self.calls = []

    def signed_request(self, path, query=None):
        self.calls.append((path, query))
        return {
            "result": {
                "links": [
                    {"linkid": "10", "is_article": 1,
                     "title": "keep"},
                    {"title": "malformed"},
                    {"linkid": "11", "link_tag": 27,
                     "title": "filtered"},
                ],
                "has_more": True,
            },
        }


class CorpusPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = CorpusManifest.from_value(MANIFEST)
        self.store = CorpusStore(self.root)
        self.store.plan(self.manifest)

    def tearDown(self):
        self.temp.cleanup()

    def _page_result(self, contents):
        query = self.manifest.queries[0]
        current = Position("offset", 0)
        result_coverage = coverage(
            {"has_more": True}, count=len(contents), position=0)
        items = tuple(
            ProviderItem(
                ordinal=index,
                content=content,
                entity_key=f"post:{content['id']}",
                digest=sha256_value(content),
                eligible=True,
            )
            for index, content in enumerate(contents)
        )
        return PageResult(
            items=items,
            coverage=result_coverage,
            evidence=(PageEvidence(
                route="/bbs/app/topic/feeds",
                protocol="app",
                observed_at=1.0,
                configured_uid="101",
                identity_verified=True,
                session_revision="session-one",
            ),),
            current_position=current,
            next_position=Position("offset", len(contents)),
        )

    def test_lossless_page_keeps_provider_order_before_filters(self):
        client = PageClient()
        args = xhh.parser().parse_args([
            "feed", "7214", "--content-type", "article",
        ])
        data, scope = Browser(client).page(
            args, Budget(), lossless=True)

        self.assertEqual([item["id"] for item in data["items"]], ["10"])
        self.assertEqual(len(data["occurrences"]), 3)
        self.assertEqual(
            [row["content"]["id"] if row["content"] else None
             for row in data["occurrences"]],
            ["10", None, "11"],
        )
        self.assertEqual(
            [row["reason"] for row in data["occurrences"]],
            [None, "malformed_record", "filter_excluded"],
        )
        self.assertEqual(scope["invalid"], 1)
        self.assertEqual(scope["excluded"], 2)

    def test_page_commit_uses_one_entity_copy_and_exact_remainder(self):
        contents = [
            {"id": str(index), "title": f"body-{index}-sentinel"}
            for index in range(1, 6)
        ]
        lease = self.store.acquire_lease(
            "page-run", "page-worker", max_new_records=2)
        query = self.manifest.queries[0]
        key = frame_key(query, Position("offset", 0), self.manifest.reader)
        token = self.store.reserve_attempt(
            lease,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=lease["request_limit"],
        )
        self.store.settle_attempt(lease, token, success=True)
        delivery = self.store.commit_page(
            lease, query.query_id, token, self._page_result(contents))
        self.assertEqual(delivery["counts"]["new"], 2)
        self.assertEqual(delivery["counts"]["deferred"], 3)

        with closing(sqlite3.connect(self.store.path)) as connection:
            frame = connection.execute(
                "SELECT provider_page_size FROM corpus_frames "
                "WHERE run_id=? AND frame_key=?", ("page-run", key),
            ).fetchone()
            items = list(connection.execute(
                """SELECT ordinal,entity_key,disposition
                   FROM corpus_frame_items
                   WHERE run_id=? AND frame_key=? ORDER BY ordinal""",
                ("page-run", key),
            ))
            entities = list(connection.execute(
                """SELECT entity_key,admission_state,content_json
                   FROM corpus_entities
                   WHERE run_id=? ORDER BY entity_key""", ("page-run",),
            ))
            attempts = connection.execute(
                "SELECT COUNT(*) FROM corpus_attempts WHERE run_id=?",
                ("page-run",),
            ).fetchone()[0]
        self.assertEqual(frame[0], 5)
        self.assertEqual(
            [(row[0], row[2]) for row in items],
            [(0, "admitted"), (1, "admitted"),
             (2, "deferred"), (3, "deferred"), (4, "deferred")],
        )
        self.assertEqual(len(entities), 5)
        self.assertEqual(attempts, 1)

        with closing(sqlite3.connect(self.store.path)) as connection:
            values = []
            for table in (
                    "corpus_runs", "corpus_queries", "corpus_attempts",
                    "corpus_frames", "corpus_frame_items", "corpus_entities",
                    "corpus_memberships", "corpus_deliveries"):
                for row in connection.execute(f"SELECT * FROM {table}"):
                    values.extend(str(value) for value in row if value is not None)
        self.assertEqual(
            sum("body-1-sentinel" in value for value in values), 1)
        self.assertEqual(
            sum("body-3-sentinel" in value for value in values), 1)

        replay = self.store.pending_delivery("page-run")
        self.assertEqual(
            [item["id"] for item in replay["new_items"]], ["1", "2"])
        self.assertEqual(replay["remainders"][0]["count"], 3)
        self.store.acknowledge_delivery("page-run", delivery["revision"])
        self.assertIsNone(self.store.pending_delivery("page-run"))

        repeated = self.store.commit_page(
            lease, query.query_id, token, self._page_result(contents))
        self.assertTrue(repeated["replayed_frame"])
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM corpus_frames").fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM corpus_memberships").fetchone()[0], 2)

    def test_shared_entity_keeps_each_query_delivery_valid(self):
        shared = json.loads(json.dumps(MANIFEST))
        shared["run_id"] = "shared-run"
        shared["queries"].append({
            "query_id": "feed-two",
            "kind": "feed",
            "target": {"topic_id": "7214"},
            "filters": {},
            "limits": {"pages": 3, "new_records": 5},
        })
        manifest = CorpusManifest.from_value(shared)
        self.store.plan(manifest)
        lease = self.store.acquire_lease(
            "shared-run", "shared-worker", max_new_records=20)

        for query, contents in zip(
                manifest.queries,
                [
                    [{"id": "1", "title": "one"},
                     {"id": "2", "title": "two"}],
                    [{"id": "2", "title": "two"},
                     {"id": "3", "title": "three"}],
                ],
        ):
            key = frame_key(
                query, Position("offset", 0), manifest.reader)
            token = self.store.reserve_attempt(
                lease,
                query_id=query.query_id,
                frame_key=key,
                role="page",
                protocol="app",
                invocation_limit=lease["request_limit"],
            )
            delivery = self.store.commit_page(
                lease, query.query_id, token,
                self._page_result(contents),
            )
            replay = self.store.pending_delivery("shared-run")
            self.assertIsNotNone(replay)
            self.store.acknowledge_delivery(
                "shared-run", delivery["revision"], lease=lease)

        self.assertEqual(
            [item["id"] for item in replay["new_items"]], ["3"])
        self.assertEqual(
            [item["id"] for item in replay["updated_items"]], ["2"])
        self.assertEqual(
            replay["memberships"],
            [["feed-two", "post:2"], ["feed-two", "post:3"]],
        )

    def test_filter_exclusions_do_not_invalidate_replay(self):
        lease = self.store.acquire_lease(
            "page-run", "mixed-page", max_new_records=5)
        query = self.manifest.queries[0]
        key = frame_key(query, Position("offset", 0), self.manifest.reader)
        token = self.store.reserve_attempt(
            lease,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=lease["request_limit"],
        )
        base = self._page_result([])
        items = (
            ProviderItem(
                0, {"id": "1", "title": "article"}, "post:1",
                sha256_value({"id": "1", "title": "article"}), True),
            ProviderItem(
                1, None, None, sha256_value({"malformed": True}), False,
                "malformed_record"),
            ProviderItem(
                2, {"id": "2", "title": "dynamic"}, "post:2",
                sha256_value({"id": "2", "title": "dynamic"}), False,
                "filter_excluded"),
        )
        page = PageResult(
            items=items,
            coverage=base.coverage,
            evidence=base.evidence,
            current_position=base.current_position,
            next_position=base.next_position,
        )

        delivery = self.store.commit_page(
            lease, query.query_id, token, page)

        self.assertEqual(delivery["counts"]["provider"], 3)
        self.assertEqual(delivery["counts"]["new"], 1)
        self.assertEqual(delivery["counts"]["invalid"], 1)
        self.assertEqual(delivery["counts"]["excluded"], 1)
        with closing(sqlite3.connect(self.store.path)) as connection:
            counts = json.loads(connection.execute(
                """SELECT counts_json FROM corpus_deliveries
                   WHERE run_id=? AND revision=?""",
                ("page-run", delivery["revision"]),
            ).fetchone()[0])
            counts.pop("excluded")
            connection.execute(
                """UPDATE corpus_deliveries SET counts_json=?
                   WHERE run_id=? AND revision=?""",
                (
                    json.dumps(counts), "page-run", delivery["revision"],
                ),
            )
            connection.commit()

        before = hashlib.sha256(self.store.path.read_bytes()).digest()
        status = self.store.status("page-run")
        replay = self.store.pending_delivery("page-run")
        after = hashlib.sha256(self.store.path.read_bytes()).digest()
        self.assertEqual(before, after)
        self.assertIsNotNone(status["pending_delivery"])
        self.assertEqual(replay["counts"]["excluded"], 1)
        self.assertEqual([item["id"] for item in replay["new_items"]], ["1"])
        self.store.acknowledge_delivery(
            "page-run", delivery["revision"], lease=lease)
        self.assertIsNone(self.store.pending_delivery("page-run"))

    def test_delivery_must_reference_every_item_in_its_frame(self):
        lease = self.store.acquire_lease(
            "page-run", "complete-delivery", max_new_records=5)
        query = self.manifest.queries[0]
        key = frame_key(query, Position("offset", 0), self.manifest.reader)
        token = self.store.reserve_attempt(
            lease,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=lease["request_limit"],
        )
        delivery = self.store.commit_page(
            lease, query.query_id, token,
            self._page_result([{"id": "1", "title": "one"}]),
        )
        counts = dict(delivery["counts"])
        counts.update(new=0, updated=0)
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.execute(
                """UPDATE corpus_deliveries
                   SET entity_refs_json='{"new":[],"updated":[]}',
                       membership_refs_json='[]',counts_json=?
                   WHERE run_id=? AND revision=?""",
                (
                    json.dumps(counts), "page-run", delivery["revision"],
                ),
            )
            connection.commit()

        with self.assertRaises(CorpusConflict) as raised:
            self.store.pending_delivery("page-run")
        self.assertEqual(raised.exception.code, "corpus_delivery_invalid")

    def test_committed_stage_releases_page_content_and_rollback_keeps_it(self):
        lease = self.store.acquire_lease(
            "page-run", "stage-content", max_new_records=5)
        query = self.manifest.queries[0]
        key = frame_key(query, Position("offset", 0), self.manifest.reader)
        token = self.store.reserve_attempt(
            lease,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=lease["request_limit"],
        )
        content = {"id": "8", "title": "stage-sentinel"}
        page = self._page_result([content])
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.execute(
                """CREATE TRIGGER reject_delivery
                   BEFORE INSERT ON corpus_deliveries
                   BEGIN SELECT RAISE(ABORT, 'fixture rollback'); END""")
            connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.commit_page(lease, query.query_id, token, page)
        with closing(sqlite3.connect(self.store.path)) as connection:
            failed = connection.execute(
                """SELECT state,page_json FROM corpus_page_stages
                   WHERE run_id=? AND attempt_id=?""",
                ("page-run", token.attempt_id),
            ).fetchone()
            frames = connection.execute(
                "SELECT COUNT(*) FROM corpus_frames WHERE run_id=?",
                ("page-run",),
            ).fetchone()[0]
            connection.execute("DROP TRIGGER reject_delivery")
            connection.commit()
        self.assertEqual(failed[0], "pending")
        self.assertIn("stage-sentinel", failed[1])
        self.assertEqual(frames, 0)

        self.store.commit_page(lease, query.query_id, token)
        with closing(sqlite3.connect(self.store.path)) as connection:
            committed = connection.execute(
                """SELECT state,page_json FROM corpus_page_stages
                   WHERE run_id=? AND attempt_id=?""",
                ("page-run", token.attempt_id),
            ).fetchone()
            values = []
            for table in (
                    "corpus_runs", "corpus_queries", "corpus_attempts",
                    "corpus_frames", "corpus_frame_items", "corpus_entities",
                    "corpus_memberships", "corpus_deliveries",
                    "corpus_page_stages"):
                for row in connection.execute(f"SELECT * FROM {table}"):
                    values.extend(str(value) for value in row if value is not None)
        self.assertEqual(committed, ("committed", ""))
        self.assertEqual(
            sum("stage-sentinel" in value for value in values), 1)

    def test_v1_schema_migrates_without_touching_legacy_snapshots(self):
        Store(self.root).save(
            "101",
            "legacy",
            "read:fixture",
            {"items": [{"id": "legacy"}]},
            {"status": "complete"},
            {"path": "/fixture"},
            pending_response={"data": {"items": [{"id": "pending"}]}},
        )
        with closing(sqlite3.connect(self.store.path)) as connection:
            legacy_before = list(connection.execute(
                "SELECT * FROM snapshots ORDER BY uid,name"))
            connection.execute("DROP TABLE corpus_page_stages")
            connection.execute(
                "UPDATE corpus_schema_meta SET version=1 WHERE singleton=1")
            connection.commit()

        migrated = CorpusStore(self.root)
        migrated.initialize()

        with closing(sqlite3.connect(migrated.path)) as connection:
            legacy_after = list(connection.execute(
                "SELECT * FROM snapshots ORDER BY uid,name"))
            version = connection.execute(
                "SELECT version FROM corpus_schema_meta WHERE singleton=1"
            ).fetchone()[0]
            stage_table = connection.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='corpus_page_stages'"""
            ).fetchone()
        self.assertEqual(legacy_after, legacy_before)
        self.assertEqual(version, 2)
        self.assertEqual(stage_table, (1,))
        self.assertEqual(
            migrated.status("page-run")["run"]["manifest_sha256"],
            self.manifest.sha256(),
        )

    def test_remainder_is_consumed_without_another_frame_before_resume(self):
        contents = [
            {"id": str(index), "title": f"item-{index}"}
            for index in range(1, 6)
        ]
        lease = self.store.acquire_lease(
            "page-run", "first", max_new_records=2)
        query = self.manifest.queries[0]
        key = frame_key(query, Position("offset", 0), self.manifest.reader)
        token = self.store.reserve_attempt(
            lease,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=lease["request_limit"],
        )
        self.store.settle_attempt(lease, token, success=True)
        delivery = self.store.commit_page(
            lease, query.query_id, token, self._page_result(contents))
        self.store.acknowledge_delivery("page-run", delivery["revision"])
        self.store.release_lease(lease)

        resumed = self.store.acquire_lease(
            "page-run", "second", max_new_records=5)
        tail = self.store.consume_remainder(resumed, query.query_id)
        self.assertEqual(tail["counts"]["new"], 3)
        self.assertEqual(tail["counts"]["deferred"], 0)
        self.assertEqual(tail["frame_keys"], [key])
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM corpus_attempts").fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM corpus_frames").fetchone()[0], 1)
            self.assertEqual(connection.execute(
                """SELECT COUNT(*) FROM corpus_entities
                   WHERE admission_state='admitted'""").fetchone()[0], 5)
            self.assertIsNone(connection.execute(
                "SELECT pending_remainder_frame_key FROM corpus_queries"
            ).fetchone()[0])

    def test_duplicate_deferred_entity_consumes_every_occurrence(self):
        contents = [
            {"id": "1", "title": "one"},
            {"id": "2", "title": "two"},
            {"id": "3", "title": "three"},
            {"id": "3", "title": "three"},
        ]
        lease = self.store.acquire_lease(
            "page-run", "duplicate-first", max_new_records=2)
        query = self.manifest.queries[0]
        key = frame_key(query, Position("offset", 0), self.manifest.reader)
        token = self.store.reserve_attempt(
            lease,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=lease["request_limit"],
        )
        delivery = self.store.commit_page(
            lease, query.query_id, token, self._page_result(contents))
        self.store.acknowledge_delivery(
            "page-run", delivery["revision"], lease=lease)
        self.store.release_lease(lease)

        resumed = self.store.acquire_lease(
            "page-run", "duplicate-second", max_new_records=1)
        tail = self.store.consume_remainder(resumed, query.query_id)
        self.assertEqual(tail["counts"]["new"], 1)
        self.assertEqual(tail["counts"]["updated"], 0)
        self.assertEqual(tail["counts"]["deferred"], 0)
        self.assertIsNone(self.store.status(
            "page-run")["queries"][0]["pending_remainder_count"] or None)
        replay = self.store.pending_delivery("page-run")
        self.assertEqual(
            [item["id"] for item in replay["new_items"]], ["3"])
        self.assertEqual(replay["updated_items"], [])
        self.store.acknowledge_delivery(
            "page-run", tail["revision"], lease=resumed)
        with closing(sqlite3.connect(self.store.path)) as connection:
            rows = list(connection.execute(
                """SELECT ordinal,disposition FROM corpus_frame_items
                   WHERE run_id=? AND frame_key=? AND ordinal>=2
                   ORDER BY ordinal""",
                ("page-run", key),
            ))
        self.assertEqual(rows, [(2, "admitted"), (3, "known")])

    def test_pending_delivery_rejects_contradictory_durable_refs(self):
        lease = self.store.acquire_lease(
            "page-run", "delivery-audit", max_new_records=5)
        query = self.manifest.queries[0]
        key = frame_key(query, Position("offset", 0), self.manifest.reader)
        token = self.store.reserve_attempt(
            lease,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=lease["request_limit"],
        )
        delivery = self.store.commit_page(
            lease,
            query.query_id,
            token,
            self._page_result([{"id": "1", "title": "one"}]),
        )
        with closing(sqlite3.connect(self.store.path)) as connection:
            original = connection.execute(
                """SELECT frame_refs_json,entity_refs_json,
                          membership_refs_json,counts_json,state_json,stop_json
                   FROM corpus_deliveries
                   WHERE run_id=? AND revision=?""",
                ("page-run", delivery["revision"]),
            ).fetchone()

        mutations = [
            ("frame ref", 0, "[\"missing-frame\"]"),
            ("entity ref", 1, '{"new":["post:missing"],"updated":[]}'),
            (
                "new count", 3,
                '{"provider":1,"new":2,"updated":0,"deferred":0,"invalid":0}',
            ),
            ("membership ref", 2, "[]"),
            (
                "state json", 4,
                '{"run_state":"blocked_human","query_state":"failed"}',
            ),
            ("stop json", 5, '{"code":"fake_stop"}'),
            ("empty stop json", 5, ""),
            ("serialized null stop json", 5, "null"),
        ]
        for label, index, replacement in mutations:
            with self.subTest(label=label):
                with closing(sqlite3.connect(self.store.path)) as connection:
                    columns = (
                        "frame_refs_json", "entity_refs_json",
                        "membership_refs_json", "counts_json",
                        "state_json", "stop_json",
                    )
                    connection.execute(
                        f"UPDATE corpus_deliveries SET {columns[index]}=? "
                        "WHERE run_id=? AND revision=?",
                        (replacement, "page-run", delivery["revision"]),
                    )
                    connection.commit()
                with self.assertRaises(CorpusConflict):
                    self.store.pending_delivery("page-run")
                with self.assertRaises(CorpusConflict):
                    self.store.acknowledge_delivery(
                        "page-run", delivery["revision"], lease=lease)
                self.assertIsNotNone(self.store.status(
                    "page-run")["pending_delivery"])
                with closing(sqlite3.connect(self.store.path)) as connection:
                    columns = (
                        "frame_refs_json", "entity_refs_json",
                        "membership_refs_json", "counts_json",
                        "state_json", "stop_json",
                    )
                    connection.execute(
                        f"UPDATE corpus_deliveries SET {columns[index]}=? "
                        "WHERE run_id=? AND revision=?",
                        (original[index], "page-run", delivery["revision"]),
                    )
                    connection.commit()

    def test_pending_delivery_rejects_tampered_entity_content(self):
        lease = self.store.acquire_lease(
            "page-run", "content-audit", max_new_records=5)
        query = self.manifest.queries[0]
        key = frame_key(query, Position("offset", 0), self.manifest.reader)
        token = self.store.reserve_attempt(
            lease,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=lease["request_limit"],
        )
        delivery = self.store.commit_page(
            lease,
            query.query_id,
            token,
            self._page_result([{"id": "1", "title": "original"}]),
        )
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.execute(
                """UPDATE corpus_entities SET content_json=?
                   WHERE run_id=? AND entity_key=?""",
                (
                    '{"id":"1","title":"tampered"}',
                    "page-run",
                    "post:1",
                ),
            )
            connection.commit()

        with self.assertRaises(CorpusConflict):
            self.store.pending_delivery("page-run")
        with self.assertRaises(CorpusConflict):
            self.store.acknowledge_delivery(
                "page-run", delivery["revision"], lease=lease)
        self.assertIsNotNone(self.store.status(
            "page-run")["pending_delivery"])

    def test_pending_delivery_rejects_a_lost_remainder_pointer(self):
        lease = self.store.acquire_lease(
            "page-run", "remainder-audit", max_new_records=1)
        query = self.manifest.queries[0]
        key = frame_key(query, Position("offset", 0), self.manifest.reader)
        token = self.store.reserve_attempt(
            lease,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=lease["request_limit"],
        )
        delivery = self.store.commit_page(
            lease,
            query.query_id,
            token,
            self._page_result([
                {"id": "1", "title": "one"},
                {"id": "2", "title": "two"},
                {"id": "3", "title": "three"},
            ]),
        )
        self.assertEqual(delivery["counts"]["deferred"], 2)
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.execute(
                """UPDATE corpus_queries
                   SET pending_remainder_frame_key=NULL
                   WHERE run_id=? AND query_id=?""",
                ("page-run", query.query_id),
            )
            connection.commit()

        with self.assertRaises(CorpusConflict):
            self.store.pending_delivery("page-run")
        with self.assertRaises(CorpusConflict):
            self.store.acknowledge_delivery(
                "page-run", delivery["revision"], lease=lease)
        self.assertIsNotNone(self.store.status(
            "page-run")["pending_delivery"])

    def test_second_remainder_pass_reaching_query_quota_completes_run(self):
        value = json.loads(json.dumps(MANIFEST))
        value["run_id"] = "second-quota-run"
        value["queries"][0]["limits"]["new_records"] = 3
        manifest = CorpusManifest.from_value(value)
        store = CorpusStore(self.root)
        store.plan(manifest)
        query = manifest.queries[0]
        key = frame_key(query, Position("offset", 0), manifest.reader)
        contents = [
            {"id": str(index), "title": f"item-{index}"}
            for index in range(1, 6)
        ]

        first = store.acquire_lease(
            "second-quota-run", "quota-first", max_new_records=2)
        token = store.reserve_attempt(
            first,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=first["request_limit"],
        )
        page = self._page_result(contents)
        delivery = store.commit_page(first, query.query_id, token, page)
        store.acknowledge_delivery(
            "second-quota-run", delivery["revision"], lease=first)
        store.release_lease(first)

        second = store.acquire_lease(
            "second-quota-run", "quota-second", max_new_records=1)
        tail = store.consume_remainder(second, query.query_id)
        self.assertEqual(tail["counts"]["new"], 1)
        self.assertEqual(tail["counts"]["deferred"], 2)
        status = store.status("second-quota-run")
        self.assertEqual(status["queries"][0]["state"], "quota_reached")
        self.assertEqual(status["run"]["state"], "complete")
        store.acknowledge_delivery(
            "second-quota-run", tail["revision"], lease=second)
        store.release_lease(second)
        with self.assertRaises(CorpusLocked):
            store.acquire_lease("second-quota-run", "quota-third")

    def test_v1_terminal_quota_pointer_is_normalized_for_pending_delivery(
            self):
        value = json.loads(json.dumps(MANIFEST))
        value["run_id"] = "legacy-quota-run"
        value["queries"][0]["limits"]["new_records"] = 3
        manifest = CorpusManifest.from_value(value)
        store = CorpusStore(self.root)
        store.plan(manifest)
        query = manifest.queries[0]
        key = frame_key(query, Position("offset", 0), manifest.reader)
        contents = [
            {"id": str(index), "title": f"item-{index}"}
            for index in range(1, 6)
        ]

        first = store.acquire_lease(
            "legacy-quota-run", "legacy-first", max_new_records=2)
        token = store.reserve_attempt(
            first,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=first["request_limit"],
        )
        delivery = store.commit_page(first, query.query_id, token,
                                     self._page_result(contents))
        store.acknowledge_delivery(
            "legacy-quota-run", delivery["revision"], lease=first)
        store.release_lease(first)

        second = store.acquire_lease(
            "legacy-quota-run", "legacy-second", max_new_records=1)
        tail = store.consume_remainder(second, query.query_id)
        self.assertEqual(tail["counts"]["deferred"], 2)
        self.assertEqual(
            store.status("legacy-quota-run")["queries"][0]["state"],
            "quota_reached",
        )
        store.release_lease(second)

        with closing(sqlite3.connect(store.path)) as connection:
            connection.execute(
                """UPDATE corpus_queries
                   SET pending_remainder_frame_key=?
                   WHERE run_id=? AND query_id=?""",
                (key, "legacy-quota-run", query.query_id),
            )
            connection.execute("DROP TABLE corpus_page_stages")
            connection.execute(
                "UPDATE corpus_schema_meta SET version=1 WHERE singleton=1")
            connection.commit()

        resumed = CorpusStore(self.root)
        resumed.initialize()
        with closing(sqlite3.connect(resumed.path)) as connection:
            pointer = connection.execute(
                """SELECT pending_remainder_frame_key FROM corpus_queries
                   WHERE run_id=? AND query_id=?""",
                ("legacy-quota-run", query.query_id),
            ).fetchone()[0]
        self.assertIsNone(pointer)

        lease = resumed.acquire_lease(
            "legacy-quota-run", "legacy-replay")
        replay = resumed.pending_delivery("legacy-quota-run")
        self.assertEqual(replay["revision"], tail["revision"])
        resumed.acknowledge_delivery(
            "legacy-quota-run", replay["revision"], lease=lease)
        self.assertIsNone(resumed.pending_delivery("legacy-quota-run"))
        resumed.release_lease(lease)

    def test_pending_delivery_requires_contiguous_item_ordinals(self):
        lease = self.store.acquire_lease(
            "page-run", "ordinal-audit", max_new_records=5)
        query = self.manifest.queries[0]
        key = frame_key(query, Position("offset", 0), self.manifest.reader)
        token = self.store.reserve_attempt(
            lease,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=lease["request_limit"],
        )
        delivery = self.store.commit_page(
            lease,
            query.query_id,
            token,
            self._page_result([{"id": "1", "title": "one"}]),
        )
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.execute(
                """UPDATE corpus_frame_items SET ordinal=7
                   WHERE run_id=? AND frame_key=? AND ordinal=0""",
                ("page-run", key),
            )
            connection.commit()

        with self.assertRaises(CorpusConflict):
            self.store.pending_delivery("page-run")
        with self.assertRaises(CorpusConflict):
            self.store.acknowledge_delivery(
                "page-run", delivery["revision"], lease=lease)
        self.assertIsNotNone(self.store.status(
            "page-run")["pending_delivery"])

    def test_invalid_page_ordinals_are_rejected_before_durable_commit(self):
        lease = self.store.acquire_lease(
            "page-run", "ordinal-boundary", max_new_records=5)
        query = self.manifest.queries[0]
        current = Position("offset", 0)
        key = frame_key(query, current, self.manifest.reader)
        token = self.store.reserve_attempt(
            lease,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=lease["request_limit"],
        )
        content = {"id": "1", "title": "one"}
        page = PageResult(
            items=(ProviderItem(
                ordinal=7,
                content=content,
                entity_key="post:1",
                digest=sha256_value(content),
                eligible=True,
            ),),
            coverage=coverage(
                {"has_more": False}, count=1, position=0),
            evidence=(PageEvidence(
                "/bbs/app/topic/feeds", "app", 1.0, "101", True,
                "session-one"),),
            current_position=current,
            next_position=None,
        )

        with self.assertRaises(CorpusConflict):
            self.store.commit_page(lease, query.query_id, token, page)
        with closing(sqlite3.connect(self.store.path)) as connection:
            counts = [
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE run_id=?",
                    ("page-run",),
                ).fetchone()[0]
                for table in (
                    "corpus_frames", "corpus_frame_items",
                    "corpus_entities", "corpus_deliveries",
                )
            ]
        self.assertEqual(counts, [0, 0, 0, 0])

    def test_malformed_occurrence_is_kept_as_reference_evidence(self):
        lease = self.store.acquire_lease("page-run", "malformed")
        query = self.manifest.queries[0]
        key = frame_key(query, Position("offset", 0), self.manifest.reader)
        token = self.store.reserve_attempt(
            lease,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=lease["request_limit"],
        )
        self.store.settle_attempt(lease, token, success=True)
        result = self._page_result([])
        result = PageResult(
            items=(ProviderItem(
                ordinal=0,
                content=None,
                entity_key=None,
                digest=sha256_value({"bad": "record"}),
                eligible=False,
                reason="malformed_record",
            ),),
            coverage=result.coverage,
            evidence=result.evidence,
            current_position=result.current_position,
            next_position=result.next_position,
        )
        delivery = self.store.commit_page(
            lease, query.query_id, token, result)
        self.assertEqual(delivery["counts"]["invalid"], 1)
        with closing(sqlite3.connect(self.store.path)) as connection:
            row = connection.execute(
                "SELECT entity_key,item_digest,"
                "disposition,reason FROM corpus_frame_items").fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(row[2:], ("invalid", "malformed_record"))

    def test_reader_uses_browser_page_and_keeps_invalid_occurrences(self):
        client = PageClient()
        lease = self.store.acquire_lease("page-run", "reader-worker")
        query = self.manifest.queries[0]
        position = Position("offset", 0)
        key = frame_key(query, position, self.manifest.reader)
        budget = CorpusBudget(
            self.store,
            lease,
            query_id=query.query_id,
            frame_key=key,
        )
        page = CorpusReader(xhh.parser, Browser(client)).read(
            query, position, self.manifest.reader.page_size, budget)
        self.assertEqual(len(page.items), 3)
        self.assertEqual(
            [item.reason for item in page.items],
            [None, "malformed_record", None],
        )
        self.assertEqual(len(client.calls), 1)
        delivery = self.store.commit_page(
            lease, query.query_id, budget.last_token, page)
        self.assertEqual(delivery["counts"]["provider"], 3)
        self.assertEqual(delivery["counts"]["new"], 2)
        self.assertEqual(delivery["counts"]["invalid"], 1)


if __name__ == "__main__":
    unittest.main()
