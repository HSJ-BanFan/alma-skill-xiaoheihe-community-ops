from __future__ import annotations

import base64
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(os.environ.get("XHH_TEST_SKILL_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT / "scripts"), str(Path(__file__).resolve().parent)]

import test_tools


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


if __name__ == "__main__":
    unittest.main()
