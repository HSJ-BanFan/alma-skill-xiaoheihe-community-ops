from __future__ import annotations

import io
from pathlib import Path

from assets import atomic, digest, owned
from emoji_catalog import EmojiError, cache_path
from xhh_sdk.exceptions import XhhError

MAX_EMOJIS = 24
SHEET_COLUMNS = 6
EMOJI_SIZE = 96
PADDING = 12
LABEL_HEIGHT = 20
BACKGROUND = (225, 228, 232, 255)
INK = (20, 23, 28, 255)


def _sprite(Image, url, data_dir, fetcher):
    try:
        fetched = fetcher(url, data_dir)
    except XhhError as error:
        raise EmojiError("asset_unavailable", str(error)) from None
    try:
        file = owned(Path(data_dir), fetched["source_file"])
        expected = fetched["source_sha256"]
    except XhhError:
        raise EmojiError("path_escape", "emoji sprite file escapes the data directory") from None
    except (KeyError, TypeError):
        raise EmojiError("corrupt_asset", "emoji sprite fetch result is incomplete") from None
    try:
        data = file.read_bytes()
        if digest(data) != expected:
            raise ValueError
        with Image.open(io.BytesIO(data)) as image:
            image.seek(0)
            pixels = image.convert("RGBA")
    except Exception:
        raise EmojiError("corrupt_asset", "emoji sprite source failed its digest or cannot be decoded") from None
    return pixels, {"url": url, "source_sha256": expected, "width": pixels.width, "height": pixels.height}


def preview(catalog, emoji_ids, data_dir, *, fetcher=None):
    if (not isinstance(emoji_ids, (list, tuple)) or not 1 <= len(emoji_ids) <= MAX_EMOJIS
            or len(set(map(str, emoji_ids))) != len(emoji_ids)):
        raise EmojiError("invalid_selection", f"preview takes 1 to {MAX_EMOJIS} distinct emoji IDs")
    items = [catalog.get(emoji_id) for emoji_id in emoji_ids]
    target = cache_path(data_dir, "previews")
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise EmojiError("pillow_missing", "emoji previews require Pillow; install requirements-media.txt") from None
    if fetcher is None:
        from assets import fetch as fetcher
    sprites, evidence = {}, []
    for sprite in (item["sprite"] for item in items):
        if sprite["url"] in sprites:
            continue
        pixels, found = _sprite(Image, sprite["url"], data_dir, fetcher)
        if pixels.width % sprite["columns"] or pixels.height % sprite["rows"]:
            raise EmojiError("invalid_geometry", f"emoji sprite is {pixels.width}x{pixels.height}, "
                             f"not a {sprite['columns']}x{sprite['rows']} grid")
        sprites[sprite["url"]] = pixels
        evidence.append({**found, "columns": sprite["columns"], "rows": sprite["rows"]})
    columns = min(len(items), SHEET_COLUMNS)
    tile_width, tile_height = EMOJI_SIZE + 2 * PADDING, EMOJI_SIZE + LABEL_HEIGHT + 2 * PADDING
    sheet = Image.new("RGBA", (columns * tile_width, -(-len(items) // columns) * tile_height), BACKGROUND)
    draw, font, tiles = ImageDraw.Draw(sheet), ImageFont.load_default(), []
    for index, item in enumerate(items):
        sprite, pixels = item["sprite"], sprites[item["sprite"]["url"]]
        width, height = pixels.width // sprite["columns"], pixels.height // sprite["rows"]
        left, top = sprite["column"] * width, sprite["row"] * height
        emoji = pixels.crop((left, top, left + width, top + height)).resize((EMOJI_SIZE, EMOJI_SIZE), Image.Resampling.LANCZOS)
        x, y = index % columns * tile_width + PADDING, index // columns * tile_height + PADDING
        sheet.alpha_composite(emoji, (x, y))
        box = draw.textbbox((0, 0), item["id"], font=font)
        draw.text((x + (EMOJI_SIZE - box[2] - box[0]) // 2, y + EMOJI_SIZE + (LABEL_HEIGHT - box[3] - box[1]) // 2),
                  item["id"], fill=INK, font=font)
        tiles.append({"id": item["id"], "name": item["name"], "box": [x, y, EMOJI_SIZE, EMOJI_SIZE], "label": item["id"],
                      "label_box": [x, y + EMOJI_SIZE, EMOJI_SIZE, LABEL_HEIGHT], "source_box": [left, top, width, height],
                      "sprite_url": sprite["url"]})
    stream = io.BytesIO()
    sheet.save(stream, format="PNG", optimize=True)
    data = stream.getvalue()
    sha256 = digest(data)
    target.mkdir(parents=True, exist_ok=True)
    file = cache_path(data_dir, "previews", sha256[:32] + ".png")
    if not file.is_file() or digest(file.read_bytes()) != sha256:
        atomic(file, data)
    info = catalog.info()
    return {"preview_file": str(file.resolve()), "preview_sha256": sha256, "preview_bytes": len(data),
            "width": sheet.width, "height": sheet.height, "emoji_ids": [item["id"] for item in items], "tiles": tiles,
            "catalog_version": info["catalog_version"], "source": info["source"], "sprites": evidence}
