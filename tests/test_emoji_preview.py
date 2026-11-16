import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import assets
from emoji_catalog import Catalog, EmojiError
from emoji_preview import preview
from PIL import Image

WIDE = "https://static.max-c.com/heybox_web/emoji/test/wide.png"
SMALL = "https://cdn.max-c.com/heybox_web/emoji/test/small.png"
GRIDS = {WIDE: (6, 18, 100), SMALL: (2, 2, 30)}


def colour(url, column, row):
    return (40 + 30 * column, 10 + 12 * row, 200 if url == WIDE else 90, 255)


def sprite_png(url, size=None):
    columns, rows, cell = GRIDS[url]
    image = Image.new("RGBA", size or (columns * cell, rows * cell))
    for column in range(columns):
        for row in range(rows):
            image.paste(colour(url, column, row), (column * cell, row * cell, (column + 1) * cell, (row + 1) * cell))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def item(group, native, url, column, row):
    columns, rows, _ = GRIDS[url]
    return {"id": f"{group}:{native}", "type": "native_emoji", "group": group, "native_id": native, "name": "表情" + native,
            "shortcode": f"[{group}_表情{native}]", "font_unicode": "e001", "visual_description": None, "meaning": None,
            "tags": [], "cautions": [], "annotation_source": "official_name_only", "annotation_status": "unannotated",
            "sprite": {"url": url, "source_url": url, "columns": columns, "rows": rows, "column": column, "row": row},
            "usage": {"post": "creator_span", "comment": "shortcode", "reply": "shortcode"}}


ITEMS = [item("cube", "11", WIDE, 0, 0), item("cube", "12", WIDE, 1, 0), item("cube", "26", WIDE, 5, 1),
         item("cube", "181", WIDE, 0, 17), item("cube", "186", WIDE, 5, 17), item("cube", "94", WIDE, 3, 8),
         item("heygirl", "11", SMALL, 0, 0), item("heygirl", "22", SMALL, 1, 1)]


class Response:
    def __init__(self, data):
        self.data = data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, *args):
        return self.data


class PreviewTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "data"
        self.root.mkdir()
        catalog_file = Path(folder.name) / "catalog.json"
        source = {"kind": "official_creator_assets", "js_url": "https://static.max-c.com/creator/a.js",
                  "css_url": "https://static.max-c.com/creator/a.css", "js_sha256": "a" * 64, "css_sha256": "b" * 64,
                  "observed_at": "2026-11-15T00:00:00+00:00", "render_contract": "creator_span_and_shortcode", "live_post_verified": False}
        catalog_file.write_text(json.dumps({"schema_version": 1, "catalog_version": "creator-test", "source": source, "items": ITEMS},
                                           ensure_ascii=False), encoding="utf-8")
        self.catalog = Catalog(catalog_file=catalog_file)
        self.requests = []
        self.bodies = {url: sprite_png(url) for url in GRIDS}

    def http(self, url, root):
        def opener(request, timeout):
            self.requests.append(request.full_url)
            self.assertIsNone(request.get_header("Cookie"))
            return Response(self.bodies[request.full_url])
        return assets.fetch(url, root, opener=opener)

    def stored(self, data, *, sha256=None, folder=None):
        def fetcher(url, root):
            path = Path(folder or root) / "sprite.bin"
            path.write_bytes(data)
            return {"url": url, "source_file": str(path), "source_sha256": sha256 or hashlib.sha256(data).hexdigest()}
        return fetcher

    def code(self, emoji_ids, fetcher=None):
        with self.assertRaises(EmojiError) as raised:
            preview(self.catalog, emoji_ids, self.root, fetcher=fetcher or self.http)
        return raised.exception.code

    def pixels(self, result):
        file = Path(result["preview_file"])
        self.assertTrue(file.resolve().is_relative_to((self.root / "emojis" / "previews").resolve()))
        self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(), result["preview_sha256"])
        with Image.open(file) as opened:
            image = opened.convert("RGBA")
        self.assertEqual(image.size, (result["width"], result["height"]))
        return image

    def assert_tiles_show_their_cells(self, result, image):
        for tile, emoji_id in zip(result["tiles"], result["emoji_ids"]):
            sprite = self.catalog.get(emoji_id)["sprite"]
            expected = colour(sprite["url"], sprite["column"], sprite["row"])
            x, y, width, height = tile["box"]
            for point in ((x + 3, y + 3), (x + width // 2, y + height // 2), (x + width - 4, y + height - 4)):
                self.assertEqual(image.getpixel(point), expected, (emoji_id, point))

    def test_single_emoji_is_cropped_from_the_source_sprite(self):
        result = preview(self.catalog, ["cube:94"], self.root, fetcher=self.http)
        image = self.pixels(result)
        self.assertEqual(result["emoji_ids"], ["cube:94"])
        self.assertEqual(result["catalog_version"], "creator-test")
        self.assertEqual(result["source"]["js_sha256"], "a" * 64)
        self.assertEqual(result["tiles"][0]["source_box"], [300, 800, 100, 100])
        self.assertEqual(result["sprites"], [{"url": WIDE, "source_sha256": hashlib.sha256(self.bodies[WIDE]).hexdigest(),
                                              "width": 600, "height": 1800, "columns": 6, "rows": 18}])
        self.assert_tiles_show_their_cells(result, image)

    def test_contact_sheet_labels_each_tile_with_its_id(self):
        ids = [found["id"] for found in ITEMS]
        result = preview(self.catalog, ids, self.root, fetcher=self.http)
        image = self.pixels(result)
        self.assertEqual(result["emoji_ids"], ids)
        self.assertEqual([(tile["id"], tile["label"]) for tile in result["tiles"]], [(emoji_id, emoji_id) for emoji_id in ids])
        self.assert_tiles_show_their_cells(result, image)
        self.assertEqual(sorted(self.requests), sorted([WIDE, SMALL]))
        self.assertEqual([sprite["url"] for sprite in result["sprites"]], [WIDE, SMALL])
        rows = {tile["box"][1] for tile in result["tiles"]}
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({tuple(tile["box"]) for tile in result["tiles"]}), len(ids))
        background = image.getpixel((0, 0))
        inks = []
        for tile in result["tiles"]:
            x, y, width, height = tile["label_box"]
            self.assertEqual(y, tile["box"][1] + tile["box"][3])
            band = image.crop((x, y, x + width, y + height))
            inks.append(sum(count for count, pixel in band.getcolors(width * height) if pixel != background))
            self.assertGreater(inks[-1], 20, tile["id"])
        self.assertGreater(inks[ids.index("heygirl:11")], inks[ids.index("cube:11")])
        again = preview(self.catalog, ids, self.root, fetcher=self.http)
        self.assertEqual((again["preview_file"], again["preview_sha256"]), (result["preview_file"], result["preview_sha256"]))
        self.assertEqual(len(self.requests), 2)
        self.assertNotEqual(preview(self.catalog, ids[::-1], self.root, fetcher=self.http)["preview_sha256"], result["preview_sha256"])

    def test_default_fetcher_is_the_platform_asset_fetch(self):
        with mock.patch.object(assets, "fetch", side_effect=self.stored(self.bodies[SMALL])) as fetch:
            result = preview(self.catalog, ["heygirl:22"], self.root)
        self.assertEqual(fetch.call_args.args, (SMALL, self.root))
        self.assert_tiles_show_their_cells(result, self.pixels(result))

    def test_corrupt_or_mismatched_sprites_are_not_previews(self):
        self.assertEqual(self.code(["cube:11"], self.stored(self.bodies[WIDE], sha256="0" * 64)), "corrupt_asset")
        self.assertEqual(self.code(["cube:11"], self.stored(b"not an image")), "corrupt_asset")
        self.assertEqual(self.code(["cube:11"], lambda url, root: {"url": url}), "corrupt_asset")
        self.assertEqual(self.code(["cube:11"], self.stored(sprite_png(WIDE, (601, 1800)))), "invalid_geometry")
        self.assertEqual(self.code(["cube:11"], self.stored(sprite_png(WIDE, (600, 1790)))), "invalid_geometry")
        self.assertFalse((self.root / "emojis").exists())

    def test_tampered_cached_sprite_is_rejected(self):
        first = preview(self.catalog, ["cube:11"], self.root, fetcher=self.http)
        cached = assets.fetch(WIDE, self.root)
        self.assertTrue(cached["cached"])
        Path(cached["source_file"]).write_bytes(self.bodies[SMALL])
        Path(first["preview_file"]).unlink()
        self.assertEqual(self.code(["cube:11"]), "asset_unavailable")
        self.assertEqual(list((self.root / "emojis" / "previews").iterdir()), [])

    def test_sprite_outside_the_data_directory_is_rejected(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        self.assertEqual(self.code(["cube:11"], self.stored(self.bodies[WIDE], folder=outside)), "path_escape")
        self.assertEqual(self.code(["cube:11"], self.stored(self.bodies[WIDE], folder=self.root / ".." / "outside")), "path_escape")
        self.assertFalse((self.root / "emojis").exists())

    def test_selection_is_validated_before_any_download(self):
        self.assertEqual(self.code([]), "invalid_selection")
        self.assertEqual(self.code("cube:11"), "invalid_selection")
        self.assertEqual(self.code(["cube:11", "cube:11"]), "invalid_selection")
        self.assertEqual(self.code(["cube:11"] * 25), "invalid_selection")
        self.assertEqual(self.code(["cube:11", "cube:999"]), "unknown_emoji")
        self.assertEqual(self.requests, [])

    def test_missing_pillow_blocks_previews_but_not_the_catalog(self):
        with mock.patch.dict(sys.modules, {"PIL": None}):
            self.assertEqual(self.code(["cube:11"]), "pillow_missing")
            self.assertEqual(self.catalog.get("cube:11")["shortcode"], "[cube_表情11]")
            self.assertIn("叹气", [found["name"] for found in Catalog().search("无奈")["items"]])
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
