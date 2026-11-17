from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .exceptions import XhhConfigError

PROTOCOLS = ("web", "app")


def _text_to_html(text: str) -> str:
    return "".join(f"<p>{html.escape(line) if line else '<br>'}</p>"
                   for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))


def plain_text(markup: str) -> str:
    markup = re.sub(r'<span\b[^>]*\bdata-emoji=([\'\"])([^\'\"]+)\1[^>]*>\s*</span>',
                    lambda match: "[" + html.unescape(match.group(2)) + "]", markup, flags=re.I)
    markup = re.sub(r"<br\s*/?>", "\n", markup, flags=re.I)
    markup = re.sub(r"</(?:p|h[1-6]|li|blockquote|div)>", "\n", markup, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


def _image_url(value: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise XhhConfigError("image URL must be absolute HTTPS")
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        raise XhhConfigError("image URL must be absolute HTTPS") from None
    if parts.scheme != "https" or not parts.hostname or parts.username is not None:
        raise XhhConfigError("image URL must be absolute HTTPS")
    return value


class _Images(HTMLParser):
    def __init__(self, info=None):
        super().__init__(convert_charrefs=True)
        self.images = []
        self.info = info or {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "img":
            attrs = dict(attrs)
            source = attrs.get("src")
            original = attrs.get("data-original")
            if source and original and source != original:
                raise XhhConfigError("image src and data-original must match")
            url = _image_url(original or source)
            info = self.info.get(url, {})
            self.images.append({"type": "img", "url": url,
                                "width": str(info["width"]) if info else attrs.get("data-width") or attrs.get("width") or "",
                                "height": str(info["height"]) if info else attrs.get("data-height") or attrs.get("height") or ""})

    handle_startendtag = handle_starttag


def _body_blocks(blocks: list[dict], kind: str, info: dict) -> tuple[list[dict], str]:
    parts, images = [], []
    seen_image = False
    for block in blocks:
        if block["type"] == "img":
            url = _image_url(block["url"])
            seen_image = True
            if kind == "article":
                attrs = 'data-type="image" data-original="{}"'.format(html.escape(url, quote=True))
                for key in ("width", "height"):
                    if url in info:
                        attrs += ' data-{}="{}"'.format(key, info[url][key])
                for key in ("width", "height", "alt"):
                    if block.get(key) is not None:
                        attrs += ' {}="{}"'.format(key, html.escape(str(block[key]), quote=True))
                parts.append("<p><img " + attrs + "></p>")
            else:
                images.append({**block, **info.get(url, {}), "url": url})
        elif block["type"] == "html":
            if kind == "image_text" and seen_image and plain_text(block["text"]).strip():
                raise XhhConfigError("interleaved images require post_format article")
            parts.append(block["text"])
        else:
            raise XhhConfigError("unsupported post body block")
    content = "".join(parts).strip()
    if kind == "article":
        parser = _Images(info)
        parser.feed(content)
        parser.close()
        return [{"type": "html", "text": content}, *parser.images], content
    return [{"type": "text", "text": plain_text(content).strip()}, *images], content


def _app_image_block(block: dict, info: dict) -> dict:
    entry = dict(block)
    url = _image_url(entry["url"])
    entry["url"] = url
    known = info.get(url)
    if known:
        entry["width"] = str(known["width"])
        entry["height"] = str(known["height"])
    return entry


@dataclass
class Post:
    """One post intent. post_format selects article or image_text.

    Legacy post_type values 1 and 3 remain input aliases. The wire shape is
    chosen per runtime: Web derives post_type from plan participation and
    link_tag from format; App uses the legacy SDK contract (link_tag 1 and
    post_type from format).
    """

    title: str = ""
    content: str = ""
    content_format: str = "html"
    hashtags: list[str] = field(default_factory=list)
    topic_ids: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    cover_url: str | None = None
    post_type: str = "1"
    visibility: str | None = None
    original: bool = True
    draft: bool = True
    edit_link_id: str | None = None
    post_plan: str | None = None
    extra_declaration: int | None = None
    post_format: str | None = None
    authorship: str | None = None
    declaration: int | None = None
    source: str | None = None
    image_info: list[dict] = field(default_factory=list)

    def image_info_map(self) -> dict:
        if not isinstance(self.image_info, list):
            raise XhhConfigError("image_info must be an array of uploaded image records")
        result = {}
        for item in self.image_info:
            if not isinstance(item, dict) or set(item) != {"url", "width", "height"}:
                raise XhhConfigError("image_info entries require only url, width and height")
            url = _image_url(item["url"])
            if any(type(item[key]) is not int or item[key] < 1 for key in ("width", "height")):
                raise XhhConfigError("image_info dimensions must be positive integers from the image or upload receipt")
            if url in result and result[url] != item:
                raise XhhConfigError("conflicting image_info for the same URL")
            result[url] = dict(item)
        return result

    def format(self) -> str:
        if self.post_type not in ("1", "3"):
            raise XhhConfigError('legacy post_type must be "1" or "3"; use post_format')
        if self.post_format not in (None, "image_text", "article"):
            raise XhhConfigError('post_format must be "image_text" or "article"')
        if self.post_format is not None and self.post_type == "3" and self.post_format != "article":
            raise XhhConfigError("post_format conflicts with legacy post_type")
        return self.post_format or ("article" if self.post_type == "3" else "image_text")

    def authored_html(self) -> str:
        return _text_to_html(self.content) if self.content_format == "text" else self.content

    def _validate_intent(self) -> None:
        if self.post_plan is not None and (
                type(self.post_plan) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.post_plan)):
            raise XhhConfigError("post_plan must be a metadata key of 1-64 letters, digits, underscores or hyphens")
        if self.extra_declaration is not None and (
                type(self.extra_declaration) is not int or self.extra_declaration not in (1, 2, 3)):
            raise XhhConfigError("extra_declaration must be an integer choice: 1, 2 or 3")
        if type(self.original) is not bool:
            raise XhhConfigError("original must be a boolean")
        if self.authorship not in (None, "original", "repost"):
            raise XhhConfigError('authorship must be "original" or "repost"')
        if self.declaration is not None and (type(self.declaration) is not int or self.declaration not in (0, 1, 2)):
            raise XhhConfigError("declaration must be an integer choice: 0, 1 or 2")
        if self.source is not None and (not isinstance(self.source, str) or not self.source.strip()):
            raise XhhConfigError("source must be a nonempty string")
        if self.cover_url is not None:
            _image_url(self.cover_url)
        if self.content_format not in ("html", "text"):
            raise XhhConfigError('SDK content_format must be "text" or "html"; compile Markdown first')

    def build(self, *, protocol: str = "app", blocks: list[dict] | None = None) -> dict:
        if protocol not in PROTOCOLS:
            raise XhhConfigError('protocol must be "web" or "app"')
        kind = self.format()
        info = self.image_info_map()
        self._validate_intent()
        if protocol == "web":
            return self._build_web(kind, info, blocks)
        return self._build_app(kind, info, blocks)

    def _build_web(self, kind: str, info: dict, blocks: list[dict] | None) -> dict:
        if self.visibility is not None and self.visibility not in ("1", "2", "3"):
            raise XhhConfigError('visibility must be "1" (everyone), "2" (followers) or "3" (self)')
        if self.declaration is not None and self.authorship is None and not self.original:
            raise XhhConfigError("declaration needs an explicit authorship choice")
        if blocks is None:
            if kind == "image_text" and self.content_format != "text":
                raise XhhConfigError("image_text requires text; use post_format article for HTML or Markdown")
            content = _text_to_html(self.content) if self.content_format == "text" else self.content
            blocks = [{"type": "html", "text": content}]
            blocks.extend({"type": "img", "url": _image_url(image)} for image in self.images)
        blocks, content = _body_blocks(blocks, kind, info)
        if not plain_text(content).strip() and not any(block["type"] == "img" for block in blocks):
            raise XhhConfigError("content is required")

        tags, seen = [], set()
        for raw in self.hashtags:
            tag = str(raw).strip().strip("#").strip()
            if tag and tag.casefold() not in seen:
                tags.append(tag)
                seen.add(tag.casefold())
        body = plain_text(content).replace("\n", " ").strip()
        topics = ",".join(str(t) for t in self.topic_ids) or ("58144" if kind == "image_text" else "")
        payload = {
            "title": self.title.strip(), "desc": body[:100],
            "link_tag": ("21" if topics == "58144" else "11") if kind == "article"
                        else ("28" if topics == "58144" else "27"),
            "post_type": "2" if self.post_plan else "1",
            "draft": "1" if self.draft else "0", "edit": "1" if self.edit_link_id else "0",
            "text": json.dumps(blocks, ensure_ascii=False, separators=(",", ":")),
            "words_count": str(len(plain_text(content).strip())),
            "hashtags": json.dumps(tags, ensure_ascii=False, separators=(",", ":")),
            "view_limit": self.visibility or "1", "schedule_ts": "0",
            "extra_declaration": str(self.extra_declaration if self.extra_declaration is not None else -1),
        }
        if topics:
            payload["topic_ids"] = topics
        if self.authorship is not None or self.original:
            payload["original"] = "0" if self.authorship == "repost" else "1"
            payload["declaration"] = str(self.declaration if self.declaration is not None else 0)
        if self.source is not None:
            payload["source"] = self.source
        if self.cover_url:
            payload["thumb"] = self.cover_url
        if self.edit_link_id:
            payload["link_id"] = str(self.edit_link_id)
        if self.post_plan is not None:
            payload["post_plan"] = self.post_plan
        return payload

    def _build_app(self, kind: str, info: dict, blocks: list[dict] | None) -> dict:
        if self.authorship is not None:
            raise XhhConfigError('legacy App adapter does not support authorship; use protocol "web"')
        if self.declaration is not None:
            raise XhhConfigError('legacy App adapter does not support declaration; use protocol "web"')
        if self.source is not None:
            raise XhhConfigError('legacy App adapter does not support source; use protocol "web"')
        if self.visibility is not None:
            if self.visibility not in ("0", "1", "2", "3"):
                raise XhhConfigError('App visibility must be "0" (public), "1", "2" or "3"')
            if kind != "article":
                raise XhhConfigError("legacy App adapter supports visibility only for explicit article format")
        if self.cover_url is not None and kind != "article":
            raise XhhConfigError("legacy App adapter needs explicit article format for a cover")

        if blocks is None:
            content = self.authored_html()
            if not self.content.strip() and not self.images:
                raise XhhConfigError("content is required")
            blocks = [{"type": "html", "text": content.strip()}] if self.content.strip() else []
            blocks.extend(_app_image_block({"type": "img", "url": _image_url(image)}, info)
                          for image in self.images)
        else:
            content = self.content
            kept = []
            for block in blocks:
                if not isinstance(block, dict):
                    raise XhhConfigError("unsupported post body block")
                if block.get("type") == "html" and isinstance(block.get("text"), str):
                    kept.append({"type": "html", "text": block["text"]})
                elif block.get("type") == "img" and isinstance(block.get("url"), str):
                    kept.append(_app_image_block(block, info))
                else:
                    raise XhhConfigError("unsupported post body block")
            blocks = kept
            if (not blocks
                    or (not any(block["type"] == "img" for block in blocks)
                        and not any(block["type"] == "html" and block["text"].strip() for block in blocks))):
                raise XhhConfigError("content is required")
            if not content.strip():
                content = "".join(block["text"] for block in blocks if block["type"] == "html")

        tags, seen = [], set()
        for raw in self.hashtags:
            tag = str(raw).strip().strip("#").strip()
            if tag and tag.casefold() not in seen:
                tags.append(f"#{tag}")
                seen.add(tag.casefold())
        body = plain_text(content).replace("\n", " ").strip()
        payload = {
            "title": self.title.strip(),
            "desc": body[:100],
            "link_tag": "1",
            "post_type": "3" if kind == "article" else "1",
            "draft": "1" if self.draft else "0",
            "text": json.dumps(blocks, ensure_ascii=False),
            "words_count": str(len(plain_text(content).strip())),
        }
        if tags:
            payload["hashtags"] = json.dumps(tags, ensure_ascii=False)
        if self.topic_ids:
            payload["topic_ids"] = ",".join(str(t) for t in self.topic_ids)
        if self.original:
            payload["original"] = "1"
            payload["declaration"] = "原创首发"
        if self.cover_url:
            payload["thumb"] = self.cover_url
        if self.visibility is not None:
            payload["view_limit"] = str(self.visibility)
        if self.edit_link_id:
            payload["edit"] = "1"
            payload["link_id"] = str(self.edit_link_id)
        if self.post_plan is not None:
            payload["post_plan"] = self.post_plan
        if self.extra_declaration is not None:
            payload["extra_declaration"] = str(self.extra_declaration)
        return payload
