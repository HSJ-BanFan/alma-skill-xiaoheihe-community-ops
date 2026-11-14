import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from assets import fetch, platform_url
from xhh_sdk.exceptions import XhhConfigError
from PIL import Image


class Response:
    def __init__(self, data):
        self.data = data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, *args):
        return self.data


class AssetTests(unittest.TestCase):
    def test_preview_and_cache_have_verified_bytes(self):
        image = Image.new("RGB", (2000, 1000), "blue")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        calls = []
        def opener(request, timeout):
            calls.append(request)
            self.assertIsNone(request.get_header("Cookie"))
            return Response(stream.getvalue())
        with tempfile.TemporaryDirectory() as temporary:
            result = fetch("https://imgheybox.max-c.com/a.png", Path(temporary), opener=opener)
            self.assertEqual((result["preview_width"], result["preview_height"]), (1600, 800))
            self.assertTrue(Path(result["preview_file"]).is_file())
            self.assertTrue(fetch(result["url"], Path(temporary), opener=opener)["cached"])
            self.assertEqual(len(calls), 1)
            Path(result["preview_file"]).write_bytes(b"changed")
            with self.assertRaises(XhhConfigError):
                fetch(result["url"], Path(temporary), opener=opener)

    def test_gif_preserves_original_and_labels_first_frame(self):
        images = [Image.new("RGB", (4, 4), color) for color in ("red", "blue")]
        stream = io.BytesIO()
        images[0].save(stream, format="GIF", save_all=True, append_images=images[1:], duration=100)
        with tempfile.TemporaryDirectory() as temporary:
            result = fetch("https://imgheybox.max-c.com/a.gif", Path(temporary), opener=lambda *a, **k: Response(stream.getvalue()))
            self.assertTrue(result["animated"])
            self.assertEqual(result["preview_scope"], "first_frame")
            self.assertEqual(Path(result["source_file"]).read_bytes(), stream.getvalue())

    def test_foreign_or_credentialed_urls_are_rejected(self):
        for url in ("http://imgheybox.max-c.com/a", "https://example.com/a", "https://user:pass@imgheybox.max-c.com/a"):
            with self.assertRaises(XhhConfigError):
                platform_url(url)

    def test_non_image_cannot_be_used_as_preview(self):
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(XhhConfigError):
            fetch("https://imgheybox.max-c.com/a.png", Path(temporary), opener=lambda *a, **k: Response(b"not an image"))


if __name__ == "__main__":
    unittest.main()
