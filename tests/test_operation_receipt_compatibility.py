from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("XHH_TEST_SKILL_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT / "scripts"), str(Path(__file__).resolve().parent)]

import test_tools
from test_interaction_metadata import LEGACY_SCHEMA


class OperationReceiptCompatibilityTest(unittest.TestCase):
    setUp = test_tools.ToolsTest.setUp
    tearDown = test_tools.ToolsTest.tearDown
    run_cli = test_tools.ToolsTest.run_cli
    sends = test_tools.ToolsTest.sends

    def test_upload_replay_and_offline_lookup_keep_usable_receipt(self):
        image = self.folder / "pixel.png"
        image.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7xkAAAAASUVORK5CYII="))
        code, first = self.run_cli("upload", str(image), "--operation-id", "receipt-image", "--confirm")
        self.assertEqual(code, 0)
        expected = first["data"]["receipt"]
        self.assertEqual(expected["url"], "https://imgheybox.max-c.com/fixture.png")
        self.assertEqual((expected["width"], expected["height"]), (1, 1))
        before = len(self.sends())
        code, replay = self.run_cli("upload", str(image), "--operation-id", "receipt-image", "--confirm")
        self.assertEqual(code, 0)
        self.assertTrue(replay["data"]["replayed"])
        self.assertEqual(replay["data"]["receipt"], expected)
        code, lookup = self.run_cli("operation", "receipt-image")
        self.assertEqual(code, 0)
        self.assertEqual(lookup["data"]["receipt"], expected)
        self.assertEqual(len(self.sends()), before)

    def test_v1_migration_keeps_upload_receipt_and_original_operation_identity(self):
        folder = self.folder / "data"
        folder.mkdir()
        expected = {"url": "https://imgheybox.max-c.com/fixture.png", "key": "/fixture.png",
                    "bucket": "fixture-12345", "region": "ap-shanghai", "bytes": 68,
                    "width": 1, "height": 1, "file": str(self.folder / "pixel.png")}
        fingerprint = hashlib.sha256(b"legacy upload request").hexdigest()
        with closing(sqlite3.connect(folder / "operations.sqlite3")) as database, database:
            for statement in LEGACY_SCHEMA:
                database.execute(statement)
            database.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?)", ("101", "legacy-image", fingerprint,
                "upload", "returned", json.dumps({**expected, "text": "discarded legacy body", "raw": {"body": "discarded legacy body"}}), 123))
            database.execute("PRAGMA user_version=1")
        code, lookup = self.run_cli("operation", "legacy-image")
        self.assertEqual(code, 0)
        self.assertEqual(lookup["data"]["receipt"], expected)
        with closing(sqlite3.connect(folder / "operations.sqlite3")) as database:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(database.execute("SELECT fingerprint,status,updated_at FROM operations").fetchone(),
                             (fingerprint, "returned", 123))
            self.assertNotIn("discarded legacy body", database.execute("SELECT receipt FROM operations").fetchone()[0])
        self.assertEqual(self.sends(), [])


if __name__ == "__main__":
    unittest.main()
