"""Caller-provided image stickers with content-addressed local storage."""
from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from assets import MAX_PIXELS, atomic, owned
from xhh_sdk.exceptions import XhhConfigError

MAX_STICKER_BYTES = 10 * 1024 * 1024


def _root(data_dir):
    return Path(data_dir) / "stickers"


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"sticker:[a-f0-9]{64}", value):
        raise XhhConfigError("sticker ID must be returned by stickers import")
    return value.split(":", 1)[1]


def _annotation(value, name):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 4000:
        raise XhhConfigError(f"{name} must be a string of at most 4000 characters")
    return value


def _tags(values):
    if not isinstance(values, (list, tuple)) or len(values) > 64:
        raise XhhConfigError("sticker tags must be a list of at most 64 strings")
    if any(not isinstance(value, str) or not value.strip() or len(value) > 64 for value in values):
        raise XhhConfigError("sticker tags must be nonempty strings of at most 64 characters")
    return list(dict.fromkeys(value.strip() for value in values))


def _read(record, root):
    for key in ("file", "preview_file"):
        path = owned(root, record[key])
        if not path.is_file():
            raise XhhConfigError("sticker cache file is missing")
        checksum = "source_sha256" if key == "file" else "preview_sha256"
        if hashlib.sha256(path.read_bytes()).hexdigest() != record[checksum]:
            raise XhhConfigError("sticker cache digest mismatch")
    return record


def get(sticker_id, data_dir):
    digest = _identifier(sticker_id)
    root = _root(data_dir)
    database = owned(Path(data_dir), root / "index.sqlite3")
    if not database.is_file():
        raise XhhConfigError("sticker was not imported in this data directory")
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute("SELECT metadata FROM stickers WHERE digest=?", (digest,)).fetchone()
    if row is None:
        raise XhhConfigError("sticker was not imported in this data directory")
    return _read(json.loads(row[0]), Path(data_dir))


def register(file, data_dir, *, caption=None, meaning=None, tags=()):
    caption, meaning, tags = _annotation(caption, "caption"), _annotation(meaning, "meaning"), _tags(tags)
    source = Path(file)
    if not source.is_file() or not 0 < source.stat().st_size <= MAX_STICKER_BYTES:
        raise XhhConfigError("sticker must be a nonempty image file of at most 10 MiB")
    data = source.read_bytes()
    if not 0 < len(data) <= MAX_STICKER_BYTES:
        raise XhhConfigError("sticker changed or exceeds 10 MiB")
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise XhhConfigError("sticker import requires Pillow; install requirements-media.txt") from None
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ("PNG", "JPEG", "GIF"):
                raise XhhConfigError("sticker import supports PNG, JPEG and GIF")
            if image.width * image.height > MAX_PIXELS:
                raise XhhConfigError("sticker exceeds the image pixel limit")
            suffix = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif"}[image.format]
            width, height, frames = image.width, image.height, getattr(image, "n_frames", 1)
            image.seek(0)
            thumbnail = ImageOps.exif_transpose(image).convert("RGBA")
            thumbnail.thumbnail((768, 768))
            thumbnail.info.clear()
            while True:
                stream = io.BytesIO()
                thumbnail.save(stream, format="PNG", optimize=True)
                preview_data = stream.getvalue()
                if len(preview_data) <= 512 * 1024:
                    break
                thumbnail.thumbnail((max(1, thumbnail.width // 2), max(1, thumbnail.height // 2)))
            thumbnail.close()
    except XhhConfigError:
        raise
    except Exception as error:
        raise XhhConfigError("sticker cannot be decoded as an image") from error
    digest = hashlib.sha256(data).hexdigest()
    root = _root(data_dir)
    folder = owned(Path(data_dir), root / digest)
    folder.mkdir(parents=True, exist_ok=True)
    original = owned(Path(data_dir), folder / ("source" + suffix))
    preview_file = owned(Path(data_dir), folder / "preview.png")
    record = {"id": "sticker:" + digest, "type": "image_sticker", "file": str(original.resolve()),
              "source_sha256": digest, "source_bytes": len(data), "width": width, "height": height,
              "frames": frames, "animated": frames > 1, "preview_scope": "first_frame",
              "preview_file": str(preview_file.resolve()), "preview_sha256": hashlib.sha256(preview_data).hexdigest(),
              "caption": caption, "meaning": meaning, "tags": tags, "annotation_source": "caller_provided",
              "annotation_status": "provided" if caption or meaning else "unannotated",
              "usage": {"post": "image", "comment": "image", "reply": "unsupported"}}
    database = owned(Path(data_dir), root / "index.sqlite3")
    with closing(sqlite3.connect(database, timeout=10)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS stickers (digest TEXT PRIMARY KEY, metadata TEXT NOT NULL)")
            for path, content in ((original, data), (preview_file, preview_data)):
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != hashlib.sha256(content).hexdigest():
                    atomic(path, content)
            existing = connection.execute("SELECT metadata FROM stickers WHERE digest=?", (digest,)).fetchone()
            if existing:
                previous = json.loads(existing[0])
                for key, value in (("caption", caption), ("meaning", meaning)):
                    record[key] = value if value is not None else previous.get(key)
                if not tags:
                    record["tags"] = previous.get("tags", [])
                record["annotation_status"] = "provided" if record["caption"] or record["meaning"] else "unannotated"
            connection.execute("INSERT INTO stickers VALUES (?,?) ON CONFLICT(digest) DO UPDATE SET metadata=excluded.metadata",
                               (digest, json.dumps(record, ensure_ascii=False)))
    return {**record, "existing": existing is not None}


def search(data_dir, query="", *, limit=20, offset=0):
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise XhhConfigError("sticker list requires limit 1..100 and nonnegative offset")
    if not isinstance(query, str) or len(query) > 4000:
        raise XhhConfigError("sticker query must be a string of at most 4000 characters")
    root = _root(data_dir)
    database = owned(Path(data_dir), root / "index.sqlite3")
    if not database.is_file():
        return {"items": [], "total": 0, "retrieval": "keyword", "offset": offset, "limit": limit}
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        records = [json.loads(row[0]) for row in connection.execute("SELECT metadata FROM stickers ORDER BY digest")]
    words = re.findall(r"[A-Za-z0-9]+|[\u3400-\u9fff]{2,}", query.casefold())
    candidates = []
    for record in records:
        haystack = " ".join([record.get("caption") or "", record.get("meaning") or "", *record.get("tags", [])]).casefold()
        matched = [word for word in words if word in haystack]
        if words and not matched:
            continue
        candidates.append({**record, "rank_score": len(matched), "match_reasons": matched})
    candidates.sort(key=lambda item: (-item["rank_score"], item["id"]))
    return {"items": candidates[offset:offset + limit], "total": len(candidates),
            "retrieval": "keyword", "offset": offset, "limit": limit}
