"""Self-contained HTML previews using the official native sprite geometry."""
from __future__ import annotations

import base64
import hashlib
import html
import io
from pathlib import Path

from assets import fetch, owned
from emoji_catalog import Catalog
from messages import MessageError, _html_blocks, validate_prepared


def _data_uri(data, mime):
    return "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")


def write(prepared, data_dir, file, *, catalog=None, fetcher=None):
    catalog = catalog or Catalog(data_dir=data_dir)
    current = validate_prepared(prepared, data_dir, catalog=catalog)
    fetcher = fetcher or fetch
    ids = list(dict.fromkeys(current["expected"]["emoji_ids"]))
    styles, sprites, image_urls, grids = [], {}, {}, {}
    if ids:
        try:
            from PIL import Image
        except ImportError:
            raise MessageError("preview_requires_pillow", "native image previews require Pillow") from None
        for emoji_id in ids:
            item = catalog.get(emoji_id)
            sprite = item["sprite"]
            if sprite["url"] not in sprites:
                source = fetcher(sprite["url"], data_dir)
                source_file = owned(Path(data_dir), source["source_file"])
                data = source_file.read_bytes()
                if hashlib.sha256(data).hexdigest() != source["source_sha256"]:
                    raise MessageError("preview_source_changed", "native sprite digest does not match")
                with Image.open(io.BytesIO(data)) as image:
                    if image.width % sprite["columns"] or image.height % sprite["rows"]:
                        raise MessageError("preview_geometry_mismatch", "native sprite does not match its catalog grid")
                    mime = Image.MIME.get(image.format)
                if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                    raise MessageError("preview_source_invalid", "native sprite image format is unsupported")
                sprites[sprite["url"]] = _data_uri(data, mime)
            selector = ".hb-emoji-" + item["group"] + "_" + item["native_id"]
            grids.setdefault((sprite["url"], sprite["columns"], sprite["rows"]), []).append(selector)
            styles.append(selector + "{" + f"background-position:{-sprite['column'] * 20}px {-sprite['row'] * 20}px;" + "}")
        for (url, columns, rows), selectors in grids.items():
            styles.append(",".join(selectors) + "{display:inline-block;width:20px;height:20px;vertical-align:middle;"
                          + "background-image:url(" + sprites[url] + ");"
                          + f"background-size:{columns * 20}px {rows * 20}px;" + "}")
    for asset in current["preview"]["image_stickers"]:
        path = owned(Path(data_dir), asset["preview_file"])
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != asset["preview_sha256"]:
            raise MessageError("preview_source_changed", "image sticker preview digest does not match")
        image_urls[asset["id"]] = _data_uri(data, "image/png")
    blocks = _html_blocks(current["spec"]["content"], catalog, image_urls)
    body = []
    for block in blocks:
        if block["type"] == "html":
            markup = block["text"]
            for emoji_id in ids:
                item = catalog.get(emoji_id)
                label = html.escape(item["shortcode"], quote=True)
                accessible = catalog.render(emoji_id, "post").replace("<span ", '<span role="img" aria-label="' + label + '" title="' + label + '" ', 1)
                markup = markup.replace(catalog.render(emoji_id, "post"), accessible)
            body.append(markup)
        else:
            body.append('<p><img class="sticker" src="' + block["url"] + '" alt="' + html.escape(block.get("alt", ""), quote=True) + '"></p>')
    title = html.escape(current["spec"].get("title") or "内容预览")
    notice = "本地预览，尚未发送。图片贴纸动画仅展示首帧。"
    markup = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>' + title + '</title><style>'
              + 'body{max-width:760px;margin:40px auto;padding:0 24px;font:16px/1.8 sans-serif;color:#222;background:#fff}'
              + '.notice{color:#666;font-size:14px}.sticker{max-width:100%;max-height:480px}p{white-space:pre-wrap}'
              + "".join(styles) + '</style><h1>' + title + '</h1><p class="notice">' + notice + '</p><main>'
              + "".join(body) + '</main></html>')
    target = Path(file)
    target.write_text(markup, encoding="utf-8")
    return {"file": str(target.resolve()), "sha256": hashlib.sha256(markup.encode("utf-8")).hexdigest(),
            "mime_type": "text/html", "self_contained": True, "render_scope": "local_creator_sprite",
            "platform_write": False, "native_emoji_ids": ids, "prepared_fingerprint": current["fingerprint"]}
