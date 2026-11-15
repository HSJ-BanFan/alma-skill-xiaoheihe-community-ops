import json
import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import xhh
from corpus_model import (
    CorpusManifest,
    entity_key,
    observation_hash,
    sha256_value,
)
from corpus_store import CorpusConflict, CorpusStore
from observations import Store
from xhh_sdk.exceptions import XhhConfigError


MANIFEST = {
    "schema_version": 1,
    "run_id": "corpus-one",
    "account": {"alias": "fixture", "uid": "101"},
    "reader": {"protocol": "app", "page_size": 5},
    "limits": {"requests": 8, "seconds": 30, "unique_records": 20},
    "queries": [
        {
            "query_id": "feed-one",
            "kind": "feed",
            "target": {"topic_id": "7214"},
            "filters": {"content_type": "article"},
            "limits": {"pages": 2, "new_records": 5},
        },
        {
            "query_id": "search-one",
            "kind": "search",
            "target": {"query": "alpha"},
            "filters": {},
            "limits": {"pages": 1, "new_records": 5},
        },
    ],
}


class CorpusModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_canonical_hash_ignores_object_key_order(self):
        first = json.dumps(MANIFEST, ensure_ascii=False)
        reversed_manifest = {
            key: MANIFEST[key]
            for key in reversed(list(MANIFEST))
        }
        second = json.dumps(reversed_manifest, ensure_ascii=False)
        path1, path2 = self.root / "one.json", self.root / "two.json"
        path1.write_text(first, encoding="utf-8")
        path2.write_text(second, encoding="utf-8")
        self.assertEqual(
            CorpusManifest.load(path1).sha256(),
            CorpusManifest.load(path2).sha256(),
        )

    def test_unknown_secret_and_nonfinite_manifest_values_are_rejected(self):
        value = json.loads(json.dumps(MANIFEST))
        value["pkey"] = "secret"
        with self.assertRaises(XhhConfigError):
            CorpusManifest.from_value(value)
        path = self.root / "bad.json"
        path.write_text(
            json.dumps(MANIFEST).replace('"seconds": 30', '"seconds": NaN'),
            encoding="utf-8",
        )
        with self.assertRaises(XhhConfigError):
            CorpusManifest.load(path)

    def test_manifest_seconds_match_the_bounded_http_worker(self):
        accepted = json.loads(json.dumps(MANIFEST))
        accepted["limits"]["seconds"] = 120
        self.assertEqual(
            CorpusManifest.from_value(accepted).limits.seconds, 120)

        rejected = json.loads(json.dumps(MANIFEST))
        rejected["limits"]["seconds"] = 121
        with self.assertRaises(XhhConfigError):
            CorpusManifest.from_value(rejected)

    def test_manifest_schema_version_requires_the_integer_one(self):
        for invalid in (True, 1.0):
            with self.subTest(invalid=invalid):
                value = json.loads(json.dumps(MANIFEST))
                value["schema_version"] = invalid
                with self.assertRaises(XhhConfigError):
                    CorpusManifest.from_value(value)

    def test_reader_order_is_not_part_of_the_page_read_contract(self):
        value = json.loads(json.dumps(MANIFEST))
        value["reader"]["order"] = ["native"]
        with self.assertRaises(XhhConfigError):
            CorpusManifest.from_value(value)

    def test_persisted_v1_manifest_keeps_its_original_hash(self):
        store = CorpusStore(self.root)
        store.plan(CorpusManifest.from_value(MANIFEST))
        legacy = {
            "schema_version": 1,
            "run_id": "corpus-one",
            "account": {"alias": "fixture", "uid": "101"},
            "reader": {
                "protocol": "app",
                "page_size": 5,
                "order": ["native"],
            },
            "limits": {"requests": 8, "seconds": 86400, "unique_records": 20},
            "queries": json.loads(json.dumps(MANIFEST["queries"])),
        }
        legacy_hash = sha256_value(legacy)
        with closing(sqlite3.connect(store.path)) as connection:
            connection.execute(
                """UPDATE corpus_runs
                   SET schema_version=1,manifest_json=?,manifest_sha256=?,
                       reader_hash=?
                   WHERE run_id=?""",
                (
                    json.dumps(legacy), legacy_hash,
                    sha256_value(legacy["reader"]), "corpus-one",
                ),
            )
            connection.execute("DROP TABLE corpus_page_stages")
            connection.execute(
                "UPDATE corpus_schema_meta SET version=1 WHERE singleton=1")
            connection.commit()

        migrated = CorpusStore(self.root)
        migrated.initialize()

        self.assertEqual(
            migrated.status("corpus-one")["run"]["manifest_sha256"],
            legacy_hash,
        )
        loaded = migrated.manifest("corpus-one")
        self.assertEqual(loaded.sha256(), legacy_hash)
        self.assertEqual(
            loaded.reader.sha256(), sha256_value(legacy["reader"]))
        lease = migrated.acquire_lease(
            "corpus-one", "legacy-reader", max_requests=1)
        self.assertEqual(lease["time_limit_ms"], 120_000)
        self.assertEqual(
            migrated.budget_view(lease)["requests"]["limit"], 8)
        self.assertEqual(
            migrated.budget_view(lease)["seconds"]["limit"], 86400)
        migrated.release_lease(lease)
        with self.assertRaises(XhhConfigError):
            CorpusManifest.from_value(legacy)

    def test_entity_kinds_and_comment_context_are_branded(self):
        keys = {
            entity_key(kind, "123")
            for kind in ("post", "community", "user", "game")
        }
        self.assertEqual(len(keys), 4)
        self.assertNotEqual(
            entity_key("comment", "7", post_id="1", root_id="9"),
            entity_key("comment", "7", post_id="2", root_id="9"),
        )

    def test_content_hash_ignores_only_top_level_observation_metadata(self):
        first = {
            "id": "1",
            "source": {"body": "one"},
            "observed_at": 1,
            "nested": {"source": "kept"},
        }
        second = {
            "id": "1",
            "source": {"body": "two"},
            "observed_at": 2,
            "nested": {"source": "changed"},
        }
        self.assertNotEqual(observation_hash(first), observation_hash(second))
        second["nested"]["source"] = "kept"
        self.assertEqual(observation_hash(first), observation_hash(second))

    def test_plan_is_idempotent_and_run_id_conflict_rolls_back(self):
        store = CorpusStore(self.root)
        manifest = CorpusManifest.from_value(MANIFEST)
        first = store.plan(manifest)
        second = store.plan(manifest)
        self.assertEqual(first, second)
        changed = json.loads(json.dumps(MANIFEST))
        changed["limits"]["requests"] = 9
        with self.assertRaises(CorpusConflict):
            store.plan(CorpusManifest.from_value(changed))
        self.assertEqual(store.status("corpus-one"), first)

    def test_query_hash_mismatch_is_detected_offline(self):
        store = CorpusStore(self.root)
        store.plan(CorpusManifest.from_value(MANIFEST))
        with closing(sqlite3.connect(store.path)) as connection:
            connection.execute(
                """UPDATE corpus_queries SET query_sha256='tampered'
                   WHERE run_id=? AND query_id=?""",
                ("corpus-one", "feed-one"),
            )
            connection.commit()

        with self.assertRaises(CorpusConflict) as raised:
            store.status("corpus-one")
        self.assertEqual(
            raised.exception.code, "corpus_query_binding_invalid")

    def test_status_is_read_only_and_schema_is_additive(self):
        legacy = Store(self.root)
        legacy.save(
            "101", "legacy", "feed:7214",
            {"items": [{"id": "1"}]}, {"status": "partial"}, ["feed", "7214"],
            pending_response={"data": {"items": [{"id": "1"}]}},
        )
        before_path = legacy.path
        with closing(sqlite3.connect(before_path)) as connection:
            before = connection.execute(
                "SELECT * FROM snapshots").fetchone()
        store = CorpusStore(self.root)
        store.plan(CorpusManifest.from_value(MANIFEST))
        with closing(sqlite3.connect(before_path)) as connection:
            after = connection.execute(
                "SELECT * FROM snapshots").fetchone()
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
        self.assertEqual(before, after)
        self.assertIn("corpus_runs", tables)
        status = store.status("corpus-one")
        self.assertEqual(status["run"]["revision"], 0)

    def test_plan_and_status_are_offline_commands(self):
        path = self.root / "manifest.json"
        path.write_text(json.dumps(MANIFEST), encoding="utf-8")
        output = io.StringIO()
        with patch("xhh.load_config", side_effect=AssertionError(
                "offline command loaded credentials")), \
                patch("xhh.AccountStore", side_effect=AssertionError(
                    "offline command accessed accounts")), \
                contextlib.redirect_stdout(output):
            code = xhh.main([
                "--data-dir", str(self.root), "corpus", "plan", str(path),
            ])
        self.assertEqual(code, 0)
        planned = json.loads(output.getvalue())
        self.assertEqual(planned["data"]["action"], "plan")
        self.assertEqual(planned["data"]["run"]["state"], "planned")

        database = self.root / "browsing" / "snapshots.sqlite3"
        before_status = database.read_bytes()
        output = io.StringIO()
        with patch("xhh.load_config", side_effect=AssertionError(
                "offline command loaded credentials")), \
                contextlib.redirect_stdout(output):
            code = xhh.main([
                "--data-dir", str(self.root), "corpus", "status",
                "corpus-one",
            ])
        self.assertEqual(code, 0)
        status = json.loads(output.getvalue())
        self.assertEqual(status["data"]["action"], "status")
        self.assertEqual(status["data"]["run"]["state"], "planned")
        self.assertEqual(database.read_bytes(), before_status)


if __name__ == "__main__":
    unittest.main()
