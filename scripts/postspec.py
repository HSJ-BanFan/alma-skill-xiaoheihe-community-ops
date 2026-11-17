from __future__ import annotations

import dataclasses
import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from xhh_sdk.exceptions import XhhConfigError
from xhh_sdk.payload import Post

CONTENT_FORMATS = ("text", "html", "markdown")
POST_TYPES = ("1", "3")
REQUIREMENTS_FILE = Path(__file__).resolve().parents[1] / "requirements-formatting.txt"


def renderer_version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("markdown-it-py")
    except PackageNotFoundError:
        return "unknown"


def _https_url(value, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise XhhConfigError(f"{field} must be an absolute HTTPS URL")
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        raise XhhConfigError(f"{field} must be an absolute HTTPS URL") from None
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise XhhConfigError(f"{field} must be an absolute HTTPS URL")
    return value


def _sequence(value, field: str) -> list:
    if not isinstance(value, (list, tuple)):
        raise XhhConfigError(f"{field} must be a JSON array")
    return list(value)


def _markdown_module():
    try:
        from markdown_it import MarkdownIt
        return MarkdownIt
    except ImportError:
        raise XhhConfigError(
            'content_format "markdown" requires markdown-it-py==4.2.0; install it with '
            f'python -m pip install -r "{REQUIREMENTS_FILE}"') from None


def _walk_images(tokens):
    for token in tokens:
        if token.type == "image":
            yield token
        if token.children:
            yield from _walk_images(token.children)


def _normalize_newlines(source: str) -> str:
    return source.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _markdown_to_html(source: str, info: dict) -> str:
    MarkdownIt = _markdown_module()
    md = MarkdownIt("js-default")
    md.options.update({"html": False, "linkify": False, "typographer": False, "breaks": False})
    env: dict = {}
    tokens = md.parse(_normalize_newlines(source), env)
    for token in _walk_images(tokens):
        src = _https_url(token.attrGet("src"), "Markdown image src")
        token.attrSet("data-type", "image")
        token.attrSet("data-original", src)
        token.attrs.pop("src", None)
        if src in info:
            token.attrSet("data-width", str(info[src]["width"]))
            token.attrSet("data-height", str(info[src]["height"]))
    return md.renderer.render(tokens, md.options, env)


class _ImageElements(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images: list[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "img":
            self.images.append({name.lower(): value for name, value in attrs})

    handle_startendtag = handle_starttag


class _RenderedText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = False

    def handle_data(self, data):
        if data.strip():
            self.found = True


def _has_rendered_text(html: str) -> bool:
    parser = _RenderedText()
    parser.feed(html)
    parser.close()
    return parser.found


def _media_from_payload(payload: dict, cover_url, protocol: str) -> dict:
    blocks = json.loads(payload["text"])
    images: list[dict] = []
    html_present = any(block.get("type") == "html" for block in blocks)
    for block in blocks:
        if block.get("type") == "html":
            parser = _ImageElements()
            parser.feed(str(block.get("text") or ""))
            parser.close()
            for element in parser.images:
                images.append({"placement": "inline", **element})
        elif block.get("type") == "img" and block.get("url") is not None:
            if protocol == "web" and html_present:
                # Web articles mirror inline references as a metadata tail;
                # App img blocks are the real gallery and must stay visible.
                continue
            entry = {"placement": "trailing", "src": str(block["url"])}
            for name in ("alt", "title", "width", "height"):
                if block.get(name) is not None:
                    entry[name] = block[name]
            images.append(entry)
    ordered = []
    for ordinal, image in enumerate(images, 1):
        entry = {"ordinal": ordinal, "placement": image.pop("placement"),
                 "url": image.pop("src", None) or image.pop("data-original", "")}
        for name in ("alt", "title", "width", "height"):
            if name in image:
                entry[name] = image[name]
            elif "data-" + name in image:
                entry[name] = image["data-" + name]
        ordered.append(entry)
    return {"images": ordered, "image_count": len(ordered), "cover_url": cover_url,
            "platform_visibility": "not_checked"}


def _validate(spec: dict, *, protocol: str) -> dict:
    if protocol not in ("web", "app"):
        raise XhhConfigError('protocol must be "web" or "app"')
    known = {field.name for field in dataclasses.fields(Post)}
    unknown = sorted({str(key) for key in set(spec) - known})
    if unknown:
        raise XhhConfigError("unknown post field(s): " + ", ".join(unknown))
    values = dict(spec)

    fmt = values.get("content_format", "text")
    if not isinstance(fmt, str) or fmt not in CONTENT_FORMATS:
        raise XhhConfigError('content_format must be "text", "html" or "markdown"')
    values["content_format"] = fmt
    if "post_type" in values:
        if not isinstance(values["post_type"], str) or values["post_type"] not in POST_TYPES:
            raise XhhConfigError('post_type must be the string "1" (dynamic) or "3" (article)')
    if values.get("visibility") is not None:
        if protocol == "app":
            allowed, message = ("0", "1", "2", "3"), 'visibility must be "0" (public), "1", "2" or "3"'
        else:
            allowed, message = ("1", "2", "3"), 'visibility must be "1" (everyone), "2" (followers) or "3" (self)'
        if values["visibility"] not in allowed:
            raise XhhConfigError(message)
    if values.get("cover_url") is not None:
        _https_url(values["cover_url"], "cover_url")

    for name in ("title", "content"):
        if name in values and not isinstance(values[name], str):
            raise XhhConfigError(f"{name} must be a string")
    if "original" in values and not isinstance(values["original"], bool):
        raise XhhConfigError("original must be a boolean")
    for name in ("hashtags", "topic_ids"):
        if name in values:
            for item in _sequence(values[name], name):
                if not isinstance(item, (str, int)) or isinstance(item, bool):
                    raise XhhConfigError(f"{name} entries must be strings")
    if "images" in values:
        for image in _sequence(values["images"], "images"):
            _https_url(image, "images entries")
    values["post_format"] = Post(**values).format()
    if values["post_format"] != "article" and fmt == "markdown":
        raise XhhConfigError('content_format "markdown" requires post_format "article"')
    if values["post_format"] == "image_text" and fmt != "text" and protocol == "web":
        raise XhhConfigError('HTML and Markdown require post_format "article"; image_text uses text')
    return values


def prepare_post(spec: dict, *, draft: bool, protocol: str = "web") -> Post:
    if not isinstance(spec, dict):
        raise XhhConfigError("post spec must be a JSON object")
    values = _validate(spec, protocol=protocol)
    values["draft"] = bool(draft)
    if values.get("content_format") == "markdown":
        content = values.get("content", "")
        if not content.strip():
            raise XhhConfigError("content is required")
        rendered = _markdown_to_html(content, Post(**values).image_info_map())
        if not _has_rendered_text(rendered):
            raise XhhConfigError("Markdown content requires visible text in addition to images")
        values["content"] = rendered
        values["content_format"] = "html"
    post = Post(**values)
    post.build(protocol=protocol)
    return post


def preview_post(spec: dict, *, public: bool = False, protocol: str = "web") -> dict:
    if not isinstance(spec, dict):
        raise XhhConfigError("post spec must be a JSON object")
    post = prepare_post(spec, draft=not public, protocol=protocol)
    payload = post.build(protocol=protocol)
    return {"spec": dataclasses.asdict(post), "payload": payload,
            "media": _media_from_payload(payload, post.cover_url, protocol)}
