import builtins
import os
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sticker_store
from xhh_sdk.exceptions import XhhConfigError

try:
    from PIL import Image
except ImportError:
    Image = None


@unittest.skipIf(Image is None, "Pillow is optional")
class StickerStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.source = self.folder / "caller image.png"
        with Image.new("RGB", (30, 20), "red") as image:
            image.save(self.source)
        self.data = self.folder / "isolated data"

    def tearDown(self):
        self.temporary.cleanup()

    def test_registration_is_portable_and_preserves_annotations(self):
        first = sticker_store.register(self.source, self.data, caption="合成红色图片", meaning="无奈", tags=["无奈"])
        second = sticker_store.register(self.source, self.data)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["existing"])
        self.assertEqual(second["caption"], "合成红色图片")
        self.assertEqual(second["tags"], ["无奈"])
        self.source.unlink()
        loaded = sticker_store.get(first["id"], self.data)
        self.assertEqual((loaded["width"], loaded["height"]), (30, 20))
        self.assertEqual(loaded["usage"]["reply"], "unsupported")
        self.assertEqual(sticker_store.search(self.data, "无奈")["items"][0]["id"], first["id"])

    def test_rejected_image_and_annotations_do_not_create_state(self):
        invalid = self.folder / "invalid.png"
        invalid.write_bytes(b"not a png")
        for kwargs in ({"caption": 1}, {"tags": [False]}):
            with self.assertRaises(XhhConfigError):
                sticker_store.register(self.source, self.data, **kwargs)
        with self.assertRaises(XhhConfigError):
            sticker_store.register(invalid, self.data)
        self.assertFalse(self.data.exists())

    def test_missing_pillow_is_explicit_and_listing_remains_offline(self):
        original = builtins.__import__

        def importer(name, *args, **kwargs):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("fixture without Pillow")
            return original(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=importer):
            self.assertEqual(sticker_store.search(self.data)["items"], [])
            with self.assertRaisesRegex(XhhConfigError, "requires Pillow"):
                sticker_store.register(self.source, self.data)
        self.assertFalse(self.data.exists())

    def test_corrupted_source_or_preview_never_passes_inspection(self):
        record = sticker_store.register(self.source, self.data)
        original = Path(record["file"])
        original.write_bytes(b"changed")
        with self.assertRaisesRegex(XhhConfigError, "digest mismatch"):
            sticker_store.get(record["id"], self.data)
        sticker_store.register(self.source, self.data)
        Path(record["preview_file"]).write_bytes(b"changed")
        with self.assertRaisesRegex(XhhConfigError, "digest mismatch"):
            sticker_store.get(record["id"], self.data)

    def test_cache_metadata_cannot_reference_an_outside_file(self):
        record = sticker_store.register(self.source, self.data)
        record["file"] = str(self.source)
        with closing(sqlite3.connect(self.data / "stickers/index.sqlite3")) as connection:
            with connection:
                connection.execute("UPDATE stickers SET metadata=?", (json.dumps(record),))
        with self.assertRaisesRegex(XhhConfigError, "escapes"):
            sticker_store.get(record["id"], self.data)

    def test_concurrent_imports_share_one_valid_image_identity(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(lambda index: sticker_store.register(self.source, self.data, caption=str(index)), range(8)))
        self.assertEqual(len({record["id"] for record in records}), 1)
        self.assertEqual(sticker_store.search(self.data)["total"], 1)
        self.assertEqual(sticker_store.get(records[0]["id"], self.data)["source_sha256"], records[0]["source_sha256"])

    def test_linked_sticker_directory_cannot_write_outside_the_data_directory(self):
        self.data.mkdir()
        outside = self.folder / "outside-cache"
        outside.mkdir()
        link = self.data / "stickers"
        if os.name == "nt":
            script = self.folder / "fixture-junction.ps1"
            script.write_text('param([string]$LinkPath,[string]$TargetPath)\nNew-Item -ItemType Junction -Path $LinkPath -Target $TargetPath | Out-Null\n', encoding="utf-8")
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(script),
                                     "-LinkPath", str(link), "-TargetPath", str(outside)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaises(XhhConfigError):
                sticker_store.register(self.source, self.data)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            if os.name == "nt":
                os.rmdir(link)
            else:
                link.unlink()


if __name__ == "__main__":
    unittest.main()
