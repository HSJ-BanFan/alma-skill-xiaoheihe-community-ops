"""Typed message preparation and evidence-based delivery verification."""
from __future__ import annotations

import hashlib
import html
import json
import re
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace
from typing import get_args, get_origin, get_type_hints, Union

from emoji_catalog import Catalog, EmojiError
from operations import Journal, OperationError, json_text, numeric_id, prepare_write, verify_identity
from sticker_store import get as get_sticker
from xhh_sdk.exceptions import XhhConfigError
from xhh_sdk.interaction import _validate_text
from xhh_sdk.payload import Post

RENDERER_VERSION = "creator-span-v1"
POST_FIELDS = set(Post.__dataclass_fields__) - {"content", "content_format", "images", "draft"}
SORTS = ("hot", "time_aes", "time_desc")


def _type_schema(annotation):
    origin = get_origin(annotation)
    if origin in (types.UnionType, Union):
        return {"anyOf": [_type_schema(value) for value in get_args(annotation)]}
    if origin is list:
        return {"type": "array", "items": _type_schema(get_args(annotation)[0])}
    return {"type": {str: "string", int: "integer", bool: "boolean", type(None): "null"}[annotation]}


def draft_schema():
    properties = {key: _type_schema(annotation) for key, annotation in get_type_hints(Post).items() if key in POST_FIELDS}
    properties["post_type"]["enum"] = ["1", "3"]
    properties["visibility"] = {"type": ["string", "null"], "enum": [None, "0", "1", "2", "3"]}
    properties["extra_declaration"] = {"type": ["integer", "null"], "enum": [None, 1, 2, 3]}
    properties["original"]["default"] = False
    properties.update(schema_version={"type": "integer", "const": 1, "default": 1},
                      kind={"type": "string", "enum": ["post", "comment", "reply"]},
                      content={"type": "array", "minItems": 1, "maxItems": 500, "items": {"oneOf": [
                          {"type": "object", "properties": {"type": {"const": "text"}, "text": {"type": "string", "maxLength": 200000}},
                           "required": ["type", "text"], "additionalProperties": False},
                          {"type": "object", "properties": {"type": {"const": "native_emoji"}, "id": {"type": "string", "description": "Exact ID returned by emojis search/get."}},
                           "required": ["type", "id"], "additionalProperties": False},
                          {"type": "object", "properties": {"type": {"const": "image_sticker"}, "id": {"type": "string", "pattern": "^sticker:[a-f0-9]{64}$"},
                           "alt": {"type": "string", "maxLength": 4000}}, "required": ["type", "id"], "additionalProperties": False}]}},
                      target={"type": "object", "properties": {
                          "link_id": {"type": "string", "pattern": "^[1-9][0-9]{0,29}$"},
                          "root_id": {"type": "string", "pattern": "^[1-9][0-9]{0,29}$"},
                          "reply_id": {"type": "string", "pattern": "^[1-9][0-9]{0,29}$"},
                          "page": {"type": "integer", "minimum": 1, "default": 1},
                          "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                          "sort": {"type": "string", "enum": list(SORTS), "default": "hot"},
                          "cursor": {"type": ["string", "null"], "maxLength": 4096}},
                          "required": ["link_id"], "additionalProperties": False})
    reply_target = {"type": "object", "properties": properties["target"]["properties"],
                    "required": ["link_id", "root_id", "reply_id"], "additionalProperties": False}
    comment_target = {"type": "object", "properties": {"link_id": properties["target"]["properties"]["link_id"]},
                      "required": ["link_id"], "additionalProperties": False}
    non_post = {"anyOf": [{"required": [key]} for key in sorted(POST_FIELDS)]}
    return {"type": "object", "properties": properties, "required": ["kind", "content"], "additionalProperties": False,
            "allOf": [{"if": {"properties": {"kind": {"const": "post"}}}, "then": {"not": {"required": ["target"]}}},
                      {"if": {"properties": {"kind": {"const": "comment"}}}, "then": {"required": ["target"], "properties": {"target": comment_target}, "not": non_post}},
                      {"if": {"properties": {"kind": {"const": "reply"}}}, "then": {"required": ["target"], "properties": {"target": reply_target}, "not": non_post}}],
            "description": "Post uses topic_ids and post metadata. Comment/reply use target. Reply needs root_id/reply_id and accepts no image sticker. Comment images are attachments after text."}


def prepared_schema():
    return {"type": "object", "properties": {"schema_version": {"type": "integer", "const": 1}, "prepared_version": {"type": "integer", "const": 1},
            "spec": draft_schema(), "protocol": {"enum": ["web", "app"]}, "catalog_version": {"type": "string"},
            "catalog_fingerprint": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "renderer_version": {"type": "string"}, "image_hashes": {"type": "object"}, "fingerprint": {"type": "string"}},
            "required": ["prepared_version", "spec", "protocol", "catalog_version", "catalog_fingerprint", "renderer_version", "image_hashes", "fingerprint"],
            "description": "The complete prepared object returned by compose; send revalidates its canonical fingerprint."}


class MessageError(XhhConfigError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _hash(value):
    return hashlib.sha256(json_text(value).encode("utf-8")).hexdigest()


def _normalized_text(value):
    return re.sub(r"\s+", " ", value).strip()


def _text_hash(value):
    return hashlib.sha256(_normalized_text(value).encode("utf-8")).hexdigest()


def _content_hash(blocks, attachments=()):
    runs, text = [], []

    def flush():
        value = _normalized_text("".join(text))
        if value:
            runs.append({"type": "text", "text": value})
        text.clear()

    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            text.append(block["text"])
        elif kind == "native_emoji":
            flush()
            runs.append({"type": kind, "id": block["id"]})
        elif kind in ("image", "img"):
            flush()
            runs.append({"type": "image", "url": block["url"]})
    flush()
    for url in attachments:
        runs.append({"type": "image", "url": url})
    return _hash(runs)


def _string(value, name, maximum=200_000):
    if not isinstance(value, str) or len(value) > maximum:
        raise MessageError("invalid_message_spec", f"{name} must be a string of at most {maximum} characters")
    return value


def _number(value, name, maximum=None):
    if type(value) is not int or value < 1 or maximum is not None and value > maximum:
        raise MessageError("invalid_message_spec", f"{name} must be a positive integer" + (f" at most {maximum}" if maximum else ""))
    return value


def _target(spec, kind):
    raw = spec.get("target")
    if kind == "post":
        if "target" in spec:
            raise MessageError("invalid_message_spec", "new posts use topic_ids; target is for comments and replies")
        return None
    if not isinstance(raw, dict):
        raise MessageError("invalid_message_spec", "comments and replies require a target object")
    allowed = {"link_id"} if kind == "comment" else {"link_id", "root_id", "reply_id", "page", "limit", "sort", "cursor"}
    if set(raw) - allowed:
        raise MessageError("invalid_message_spec", "target contains unsupported fields")
    target = {"link_id": numeric_id(raw.get("link_id"))}
    if kind == "reply":
        target.update(root_id=numeric_id(raw.get("root_id")), reply_id=numeric_id(raw.get("reply_id")),
                      page=_number(raw.get("page", 1), "page"), limit=_number(raw.get("limit", 50), "limit", 100),
                      sort=raw.get("sort", "hot"), cursor=raw.get("cursor"))
        if target["sort"] not in SORTS:
            raise MessageError("invalid_message_spec", "reply sort must be hot, time_aes or time_desc")
        if target["cursor"] is not None:
            _string(target["cursor"], "cursor", 4096)
    return target


def _post_options(spec):
    options = {key: spec[key] for key in POST_FIELDS if key in spec}
    options.setdefault("original", False)
    for name in ("title", "post_type", "visibility", "cover_url", "edit_link_id", "post_plan"):
        if name in options and options[name] is not None:
            _string(options[name], name, 4000)
    if options.get("post_type", "1") not in ("1", "3"):
        raise MessageError("invalid_message_spec", "post_type must be 1 or 3")
    if options.get("visibility") not in (None, "0", "1", "2", "3"):
        raise MessageError("invalid_message_spec", "visibility must be 0, 1, 2 or 3")
    if "original" in options and type(options["original"]) is not bool:
        raise MessageError("invalid_message_spec", "original must be boolean")
    for name in ("hashtags", "topic_ids"):
        if name in options and (not isinstance(options[name], list) or len(options[name]) > 100):
            raise MessageError("invalid_message_spec", f"{name} must be a list of at most 100 items")
    if "hashtags" in options and any(not isinstance(item, str) for item in options["hashtags"]):
        raise MessageError("invalid_message_spec", "hashtags must be strings")
    if "topic_ids" in options:
        options["topic_ids"] = [numeric_id(item) for item in options["topic_ids"]]
    if options.get("edit_link_id") is not None:
        options["edit_link_id"] = numeric_id(options["edit_link_id"])
    return options


def _html_blocks(content, catalog, images):
    blocks, inline = [], []

    def flush():
        if inline:
            markup = "".join(inline)
            blocks.append({"type": "html", "text": "".join("<p>" + (line or "<br>") + "</p>" for line in markup.split("\n"))})
            inline.clear()

    for node in content:
        if node["type"] == "text":
            previous = 0
            for match in re.finditer(r"\[[^\]\n]+_[^\]\n]+\]", node["text"]):
                item = catalog.resolve_shortcode(match.group())
                if item:
                    inline.append(html.escape(node["text"][previous:match.start()]))
                    inline.append(catalog.render(item["id"], "post"))
                    previous = match.end()
            inline.append(html.escape(node["text"][previous:]))
        elif node["type"] == "native_emoji":
            inline.append(catalog.render(node["id"], "post"))
        else:
            flush()
            blocks.append({"type": "img", "url": images.get(node["id"], "pending-upload:" + node["id"]), "alt": node["alt"]})
    flush()
    return blocks


def compose(spec, data_dir, *, protocol="web", catalog=None):
    catalog = catalog or Catalog(data_dir=data_dir)
    if not isinstance(spec, dict) or spec.get("schema_version", 1) != 1 or type(spec.get("schema_version", 1)) is not int:
        raise MessageError("invalid_message_spec", "message spec requires schema_version 1")
    kind = spec.get("kind")
    if kind not in ("post", "comment", "reply") or protocol not in ("web", "app"):
        raise MessageError("invalid_message_spec", "kind must be post, comment or reply; protocol must be web or app")
    allowed = {"schema_version", "kind", "content", "target"} | (POST_FIELDS if kind == "post" else set())
    if set(spec) - allowed:
        raise MessageError("invalid_message_spec", "message spec contains unsupported fields")
    raw = spec.get("content")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 500:
        raise MessageError("invalid_message_spec", "content requires 1..500 typed nodes")
    content, text, emoji_ids, image_hashes, assets, warnings = [], [], [], {}, [], []
    comment_image_seen = False
    for node in raw:
        if not isinstance(node, dict):
            raise MessageError("invalid_message_spec", "content nodes must be objects")
        node_type = node.get("type")
        if node_type == "text" and set(node) == {"type", "text"}:
            value = _string(node["text"], "text").replace("\r\n", "\n").replace("\r", "\n")
            if kind == "comment" and comment_image_seen and value.strip():
                raise MessageError("unsupported_sticker_position", "comment image stickers are attachments; put the sticker after text")
            content.append({"type": "text", "text": value})
            text.append(value)
            for shortcode in re.findall(r"\[(?:cube|heygirl)_[^\]\n]+\]", value):
                item = catalog.resolve_shortcode(shortcode)
                if item:
                    emoji_ids.append(item["id"])
                else:
                    warnings.append({"code": "unknown_shortcode_in_text", "value": shortcode,
                                     "next_action": "Use emojis search/get and a native_emoji ID for validated native rendering."})
        elif node_type == "native_emoji" and set(node) == {"type", "id"}:
            if kind == "comment" and comment_image_seen:
                raise MessageError("unsupported_sticker_position", "comment image stickers are attachments; put the sticker last")
            try:
                item = catalog.get(node["id"])
                catalog.render(item["id"], kind)
            except EmojiError as error:
                raise MessageError(error.code, str(error)) from None
            content.append({"type": "native_emoji", "id": item["id"]})
            text.append(item["shortcode"])
            emoji_ids.append(item["id"])
        elif node_type == "image_sticker" and set(node) <= {"type", "id", "alt"} and "id" in node:
            if kind == "reply":
                raise MessageError("unsupported_sticker_target", "reply supports text and native emojis, not image stickers")
            asset = get_sticker(node["id"], data_dir)
            alt = _string(node.get("alt", asset.get("caption") or ""), "sticker alt", 4000)
            content.append({"type": "image_sticker", "id": asset["id"], "alt": alt})
            image_hashes[asset["id"]] = asset["source_sha256"]
            assets.append(asset)
            text.append("\n")
            comment_image_seen = kind == "comment"
        else:
            raise MessageError("invalid_message_spec", "nodes require text, native_emoji ID or imported image_sticker ID")
    text = "".join(text).strip()
    if kind in ("comment", "reply"):
        _validate_text(text)
        if len(assets) > 1:
            raise MessageError("unsupported_sticker_count", "a top-level comment supports one image sticker")
    options = _post_options(spec) if kind == "post" else {}
    if kind == "post":
        Post(content=text, content_format="text", images=["https://example.invalid/preflight" for _ in assets], **options).build()
    target = _target(spec, kind)
    canonical = {"schema_version": 1, "kind": kind, "content": content, **options}
    if target is not None:
        canonical["target"] = target
    info = catalog.info()
    material_keys = ("id", "group", "native_id", "name", "shortcode", "font_unicode", "sprite", "usage")
    native_materials = []
    for identifier in dict.fromkeys(emoji_ids):
        item = catalog.get(identifier)
        native_materials.append({key: item[key] for key in material_keys})
    catalog_fingerprint = _hash({"source": {key: info["source"][key] for key in ("js_sha256", "css_sha256")},
                                 "materials": native_materials})
    identity = {"spec": canonical, "protocol": protocol, "catalog_version": info["catalog_version"],
                "catalog_fingerprint": catalog_fingerprint, "renderer_version": RENDERER_VERSION, "image_hashes": image_hashes}
    local_images = {asset["id"]: Path(asset["file"]).resolve().as_uri() for asset in assets}
    preview = {"text": text, "blocks": _html_blocks(content, catalog, local_images) if kind == "post" else content,
               "native_emoji_ids": emoji_ids, "image_stickers": assets, "warnings": warnings}
    return {"schema_version": 1, "prepared_version": 1, **identity, "fingerprint": _hash(identity),
            "preview": preview, "expected": {"text_sha256": _text_hash(text), "emoji_ids": emoji_ids},
            "dry_run": True, "source": info.get("source"), "public_visibility": "not_checked"}


def validate_prepared(prepared, data_dir, *, protocol=None, catalog=None):
    if not isinstance(prepared, dict) or type(prepared.get("prepared_version")) is not int or prepared.get("prepared_version") != 1:
        raise MessageError("invalid_prepared_message", "send requires the prepared JSON returned by compose")
    required = {"spec", "protocol", "catalog_version", "catalog_fingerprint", "renderer_version", "image_hashes", "fingerprint"}
    if not required <= set(prepared):
        raise MessageError("invalid_prepared_message", "prepared message is missing its identity fields")
    if protocol is not None and prepared["protocol"] != protocol:
        raise MessageError("prepared_protocol_mismatch", "compose and send must use the same explicit protocol")
    current = compose(prepared["spec"], data_dir, protocol=prepared["protocol"], catalog=catalog)
    if current["fingerprint"] != prepared["fingerprint"]:
        raise MessageError("prepared_message_changed", "catalog, renderer, material or message changed; compose again")
    return current


class _ComposedPost(Post):
    def __init__(self, prepared, blocks, *, public):
        options = {key: value for key, value in prepared["spec"].items() if key in POST_FIELDS}
        images = [block["url"] for block in blocks if block["type"] == "img"]
        super().__init__(content=prepared["preview"]["text"], content_format="text", images=images, draft=not public, **options)
        self._blocks = blocks

    def build(self):
        payload = super().build()
        payload["text"] = json.dumps(self._blocks, ensure_ascii=False)
        return payload


def send(client, prepared, data_dir, operation_id, *, confirm=False, public=False):
    if confirm is not True:
        raise OperationError("confirmation_required")
    if type(public) is not bool:
        raise MessageError("invalid_message_spec", "public must be boolean")
    catalog = Catalog(data_dir=data_dir)
    current = validate_prepared(prepared, data_dir, protocol=client.config.protocol_mode, catalog=catalog)
    kind = current["spec"]["kind"]
    if public and kind != "post":
        raise MessageError("invalid_message_spec", "public is a post option")
    verify_identity(client)
    uid = str(client.config.heybox_id)
    context = {"prepared_fingerprint": current["fingerprint"], "kind": kind, "target": current["spec"].get("target"),
               "expected": current["expected"], "catalog_version": current["catalog_version"],
               "renderer_version": RENDERER_VERSION, "public_requested": public, "author_uid": uid}
    context.update(protocol=client.config.protocol_mode, **(current["spec"].get("target") or {}))
    identity = {"prepared_fingerprint": current["fingerprint"], "public": public}
    journal = Journal(Path(data_dir))
    if kind == "post":
        options = _post_options(current["spec"])
        if public and client.config.protocol_mode == "web" and not options.get("topic_ids"):
            raise MessageError("post_community_required", "public Web posts require topic_ids from creator-options")
        if options.get("edit_link_id"):
            client._assert_own_post(options["edit_link_id"])

        def execute():
            images = {}
            for asset in current["preview"]["image_stickers"]:
                file = Path(asset["file"])
                if hashlib.sha256(file.read_bytes()).hexdigest() != asset["source_sha256"]:
                    raise MessageError("sticker_changed", "image sticker changed after preparation")
                url = client.upload(file)["url"]
                if not isinstance(url, str) or not url.startswith("https://"):
                    raise MessageError("invalid_upload_receipt", "image upload returned no HTTPS URL")
                images[asset["id"]] = url
            blocks = _html_blocks(current["spec"]["content"], catalog, images)
            result = client.publish(_ComposedPost(current, blocks, public=public), confirm=True, upload_local=False)
            from documents import document
            expected_blocks = document({"id": result["link_id"], "text": blocks}, {}, catalog)["body"]["blocks"]
            return {"link_id": result["link_id"], "url": result["url"], "draft": result["draft"],
                    "kind": kind, "expected": {**current["expected"],
                                              "image_urls_sha256": _hash([block["url"] for block in blocks if block["type"] == "img"]),
                                              "content_sha256": _content_hash(expected_blocks)},
                    "prepared_fingerprint": current["fingerprint"], "visibility": "not_checked"}
        return journal.execute(uid, operation_id, "publish", identity, execute, identity_verified=True, context=context)
    target = current["spec"]["target"]
    directory = Path(data_dir) / "message-preparation"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="message-", dir=directory) as temporary:
        text_file = Path(temporary) / "text.txt"
        text_file.write_text(current["preview"]["text"], encoding="utf-8")
        args = SimpleNamespace(**target, text_file=text_file, image_url=None,
                               image_file=Path(current["preview"]["image_stickers"][0]["file"]) if current["preview"]["image_stickers"] else None)
        payload, execute_comment = prepare_write(client, kind, args,
            before_post=lambda request: journal.bind_submitted_request(uid, operation_id, request))

        def execute():
            result = execute_comment()
            from documents import Markup
            parsed = Markup(catalog)
            parsed.feed(current["preview"]["text"])
            parsed.flush()
            image_urls = [result["image_url"]] if result.get("image_url") else []
            return {**result, "kind": kind, "expected": {**current["expected"], "image_urls_sha256": _hash(image_urls),
                    "content_sha256": _content_hash(parsed.events, image_urls)}, "prepared_fingerprint": current["fingerprint"]}
        return journal.execute(uid, operation_id, kind, {**identity, **payload}, execute, identity_verified=True, context=context)


def verify(client, operation_id, data_dir, *, pages=3, requests=10, seconds=30):
    from browser import Browser
    import xhh

    operation = Journal(Path(data_dir)).read(str(client.config.heybox_id), operation_id)
    if operation is None:
        raise MessageError("operation_not_found", "operation does not exist for this account and data directory")
    receipt = operation.get("receipt") or {}
    expected = receipt.get("expected")
    baseline = {"operation": operation, "status": "unverified", "public_visibility": "not_checked",
                "native_render": {"status": "unknown", "reason": "API readback does not prove client icon rendering"}}
    if operation["status"] != "returned" or expected is None:
        return {**baseline, "reason": "missing_receipt_or_expected_manifest",
                "next_action": "Keep this operation ID and inspect the recorded target before retrying; do not send under a new ID."}
    from interaction_history import InteractionHistory
    history = InteractionHistory(data_dir)
    capture = xhh.browsing_capture(history, str(client.config.heybox_id), client.config.pkey)
    if receipt.get("kind") == "post":
        args = xhh.parser().parse_args(["--data-dir", str(data_dir), "post", numeric_id(receipt.get("link_id")), "--reader", "auto",
                                       "--requests", str(requests), "--seconds", str(seconds)])
        data, scope = Browser(client, capture=capture).execute(args, verify_account=True)
        observed = data.get("body") or {}
        if observed.get("status") != "available":
            return {**baseline, "reason": "body_not_observed", "coverage": scope}
        if observed.get("gaps") or observed.get("completeness") == "partial":
            return {**baseline, "reason": "readback_body_has_gaps", "coverage": scope}
        if data.get("id") != str(receipt.get("link_id")):
            return {**baseline, "status": "mismatch", "reason": "readback_post_id_mismatch", "coverage": scope}
        blocks = observed.get("blocks", [])
        text = observed.get("text", "")
        images = [block["url"] for block in blocks if block.get("type") == "image"]
        content_sha256 = _content_hash(blocks)
    else:
        target = (operation.get("context") or {}).get("target") or {}
        if receipt.get("kind") == "reply":
            arguments = ["thread", numeric_id(receipt.get("link_id")), numeric_id(receipt.get("root_id")),
                         "--around", numeric_id(receipt.get("comment_id")), "--pages", str(pages),
                         "--page", str(target.get("page", 1)), "--limit", str(target.get("limit", 50)),
                         "--sort", target.get("sort", "hot")]
        else:
            arguments = ["comments", numeric_id(receipt.get("link_id")), "--pages", str(pages), "--sort", "time_desc"]
        arguments += ["--requests", str(requests), "--seconds", str(seconds)]
        args = xhh.parser().parse_args(["--data-dir", str(data_dir), *arguments])
        data, scope = Browser(client, capture=capture).execute(args, verify_account=True)
        observed = next((node for node in data.get("items", []) if node.get("id") == str(receipt.get("comment_id"))), None)
        if observed is None:
            return {**baseline, "reason": "comment_not_observed_in_read_scope", "coverage": scope}
        if (observed.get("author") or {}).get("uid") != str(client.config.heybox_id):
            return {**baseline, "status": "mismatch", "reason": "readback_author_not_verified", "coverage": scope}
        if observed.get("gaps"):
            return {**baseline, "reason": "readback_comment_has_gaps", "coverage": scope}
        if receipt.get("kind") == "reply":
            if observed.get("reply_to_id") is None:
                return {**baseline, "reason": "reply_target_not_observed", "coverage": scope}
            if observed.get("reply_to_id") != target.get("reply_id") or observed.get("root_id") != str(receipt.get("root_id")):
                return {**baseline, "status": "mismatch", "reason": "readback_reply_target_mismatch", "coverage": scope}
        blocks, text = observed.get("content", []), observed.get("text") or ""
        images = [asset["url"] for asset in observed.get("media", [])]
        inline = {block["url"] for block in blocks if block.get("type") == "image"}
        content_sha256 = _content_hash(blocks, [url for url in images if url not in inline])
    emoji_ids = [block["id"] for block in blocks if block.get("type") == "native_emoji"]
    checks = {"text": _text_hash(text) == expected["text_sha256"], "emoji_order": emoji_ids == expected["emoji_ids"]}
    if "image_urls_sha256" in expected:
        checks["images"] = _hash(images) == expected["image_urls_sha256"]
    if "content_sha256" in expected:
        checks["content_order"] = content_sha256 == expected["content_sha256"]
    return {**baseline, "status": "matched" if all(checks.values()) else "mismatch", "checks": checks,
            "reader_scope": "authenticated_account", "identity_verified": True, "coverage": scope,
            "observed_emoji_ids": emoji_ids, "readback_content_matched": all(checks.values())}
