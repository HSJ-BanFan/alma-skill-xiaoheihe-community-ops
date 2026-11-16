import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from messages import compose, MessageError
from message_preview import write
from sticker_store import register

try:
    from PIL import Image
except ImportError:
    Image = None


@unittest.skipIf(Image is None, "Pillow is optional")
class MessagePreviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.data = self.folder / "data"
        self.data.mkdir()
        self.sprite = self.data / "fixture-sprite.png"
        with Image.new("RGBA", (60, 180), "red") as image:
            image.save(self.sprite)
        self.calls = []

    def tearDown(self):
        self.temporary.cleanup()

    def fetcher(self, url, root):
        self.calls.append(url)
        return {"source_file": str(self.sprite), "source_sha256": hashlib.sha256(self.sprite.read_bytes()).hexdigest()}

    def test_native_html_is_self_contained_escaped_and_keeps_order(self):
        prepared = compose({"kind": "post", "title": "A <script>", "content": [
            {"type": "text", "text": "before <script>alert</script> "},
            {"type": "native_emoji", "id": "cube:32"},
            {"type": "text", "text": " after "},
            {"type": "native_emoji", "id": "cube:43"}]}, self.data)
        target = self.folder / "preview.html"
        result = write(prepared, self.data, target, fetcher=self.fetcher)
        text = target.read_text(encoding="utf-8")
        self.assertTrue(result["self_contained"])
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn('aria-label="[cube_笑cry]"', text)
        self.assertEqual(text.count("data:image/png;base64,"), 1)
        self.assertLess(text.index('data-emoji="cube_笑cry"'), text.index('data-emoji="cube_叹气"'))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())

    def test_sticker_preview_embeds_the_verified_first_frame(self):
        source = self.folder / "image.png"
        with Image.new("RGB", (20, 10), "blue") as image:
            image.save(source)
        sticker = register(source, self.data, caption="合成蓝色贴纸")
        prepared = compose({"kind": "post", "content": [{"type": "text", "text": "example"},
                                                        {"type": "image_sticker", "id": sticker["id"]}]}, self.data)
        target = self.folder / "sticker.html"
        write(prepared, self.data, target, fetcher=self.fetcher)
        text = target.read_text(encoding="utf-8")
        self.assertIn("data:image/png;base64,", text)
        self.assertNotIn("file:", text)
        self.assertIn("合成蓝色贴纸", text)
        self.assertFalse(self.calls)

    def test_invalid_geometry_or_digest_cannot_create_a_preview(self):
        prepared = compose({"kind": "post", "content": [{"type": "native_emoji", "id": "cube:32"}]}, self.data)
        target = self.folder / "invalid.html"
        with Image.new("RGB", (61, 181), "blue") as image:
            image.save(self.sprite)
        with self.assertRaises(MessageError):
            write(prepared, self.data, target, fetcher=self.fetcher)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
