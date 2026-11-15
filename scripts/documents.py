"""Stable public documents, discussion nodes, and source evidence."""
from __future__ import annotations

import hashlib
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit, parse_qs

from xhh_sdk.exceptions import XhhConfigError


def identifier(value):
    value = str(value or "")
    return value if re.fullmatch(r"[1-9][0-9]{0,29}", value) else None


def entity_id(item):
    known = next((str(item[key]) for key in ("id", "uid", "topic_id", "topicid", "linkid", "appid", "folder_id")
                  if item.get(key) is not None), None)
    return known or identifier(item.get("userid"))


def merge_media(primary, supplemental):
    result = [dict(asset) for asset in primary]
    positions = {asset.get("url"): index for index, asset in enumerate(result)}
    for asset in supplemental:
        if asset["url"] in positions:
            existing = result[positions[asset["url"]]]
            for key in ("width", "height", "preview_url", "alt"):
                if existing.get(key) is None and asset.get(key) is not None:
                    existing[key] = asset[key]
        else:
            positions[asset["url"]] = len(result)
            result.append(dict(asset))
    return result


def enrich_observation(previous, latest):
    result = dict(previous)
    for key, value in latest.items():
        if value is None:
            continue
        if key == "media" and isinstance(value, list):
            result[key] = merge_media(value, previous.get(key) or [])
        elif isinstance(value, dict) and isinstance(previous.get(key), dict):
            result[key] = enrich_observation(previous[key], value)
        else:
            result[key] = value
    return result


def link_id(value):
    if identifier(value):
        return str(value)
    parsed = urlsplit(str(value))
    if parsed.scheme not in ("https", "heybox"):
        raise XhhConfigError("use a post ID or a Xiaoheihe HTTPS/heybox link")
    if parsed.scheme == "https" and (parsed.hostname not in ("xiaoheihe.cn", "www.xiaoheihe.cn", "api.xiaoheihe.cn")
                                     or parsed.username or parsed.password):
        raise XhhConfigError("post link must belong to Xiaoheihe")
    query = parse_qs(parsed.query)
    found = query.get("link_id", query.get("linkid", []))
    match = re.search(r"/(?:app/bbs/link|bbs/link|link)/(\d+)(?:/|$)", parsed.path)
    candidate = found[0] if found else match.group(1) if match else None
    if parsed.scheme == "heybox" and not candidate:
        candidate = parsed.netloc if parsed.path == "" else parsed.path.strip("/")
    if not identifier(candidate):
        raise XhhConfigError("post ID not present in this link; shortened links are not followed")
    return str(candidate)


def integer(value):
    try:
        number = int(value)
        return number if number >= 0 else None
    except (TypeError, ValueError):
        return None


def truth(value):
    if value is True or value == 1 or value == "1":
        return True
    if value is False or value == 0 or value == "0":
        return False
    return None


def source(client, route):
    return {"route": route, "protocol": client.config.protocol_mode,
            "observed_at": time.time(), "configured_uid": str(client.config.heybox_id),
            "identity_verified": False, "identity_verified_this_read": False}


class Markup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.images = [], [], []
        self.events, self.chunk = [], []

    def flush(self):
        text = "".join(self.chunk)
        if text:
            self.events.append({"type": "text", "text": text})
        self.chunk = []

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag in {"br", "p", "div", "li", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table"}:
            self.parts.append("\n")
            self.chunk.append("\n")
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])
            self.flush()
            self.events.append({"type": "link", "url": attributes["href"]})
        if tag == "img" and attributes.get("src"):
            self.images.append({"url": attributes["src"], "alt": attributes.get("alt", "")})
            self.flush()
            self.events.append({"type": "image", "url": attributes["src"], "alt": attributes.get("alt", "")})
            self.parts.append("\n")
        if tag not in {"a", "b", "i", "u", "em", "strong", "p", "div", "span", "img", "font", "color", "size",
                       "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "code", "pre",
                       "s", "strike", "del", "ins", "table", "thead", "tbody", "tr", "td", "th", "hr", "br"}:
            self.parts.append(self.get_starttag_text())
            self.chunk.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag in {"p", "div", "li", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table"}:
            self.parts.append("\n")
            self.chunk.append("\n")
        if tag in {"td", "th"}:
            self.parts.append("\t")
            self.chunk.append("\t")

    def handle_data(self, data):
        self.parts.append(data)
        self.chunk.append(data)


def plain(value):
    if not isinstance(value, str):
        return ""
    parsed = Markup()
    parsed.feed(value)
    return re.sub(r"\n{3,}", "\n\n", "".join(parsed.parts)).strip()


def author(raw):
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    return {"uid": identifier(raw.get("userid") or raw.get("user_id") or user.get("userid") or user.get("user_id") or user.get("heybox_id") or user.get("id")),
            "name": user.get("username") or user.get("nickname") or raw.get("username") or raw.get("nickname") or None,
            "avatar_url": user.get("avatar") or user.get("avartar") or raw.get("avatar") or raw.get("avartar") or None}


def media(raw, position=None):
    if isinstance(raw, str):
        raw = {"url": raw}
    if not isinstance(raw, dict):
        return None
    url = raw.get("url") or raw.get("src") or raw.get("img_url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        return None
    return {"url": url, "width": integer(raw.get("width")), "height": integer(raw.get("height")),
            "position": position, "alt": raw.get("alt") or None, "preview_url": raw.get("thumb_url") or None}


def summary(raw):
    if not isinstance(raw, dict):
        raise XhhConfigError("post record must be an object")
    row = raw.get("link", raw)
    if not isinstance(row, dict):
        raise XhhConfigError("post link must be an object")
    uid = identifier(row.get("linkid") or row.get("link_id") or row.get("id"))
    if not uid:
        raise XhhConfigError("post record has no valid ID")
    topics = row.get("topics")
    communities = [{"id": identifier(topic.get("topic_id") or topic.get("id")),
                    "name": topic.get("name") or topic.get("topic_name")}
                   for topic in topics if isinstance(topic, dict)] if isinstance(topics, list) else None
    article = truth(row.get("is_article"))
    kind = "article" if article is True or str(row.get("post_type")) == "3" else "dynamic" if article is False or str(row.get("link_tag")) == "27" else None
    raw_images = row.get("imgs") if isinstance(row.get("imgs"), list) else []
    images = [asset for asset in (media(item) for item in raw_images) if asset]
    title, excerpt, hashtags = row.get("title"), row.get("description"), row.get("hashtags")
    return {"id": uid, "url": "https://www.xiaoheihe.cn/app/bbs/link/" + uid,
            "title": plain(title) if title is not None else None,
            "excerpt": plain(excerpt) if excerpt is not None else None, "content_type": kind, "author": author(row),
            "created_at": integer(row.get("create_at")), "updated_at": integer(row.get("modify_at")),
            "communities": communities, "hashtags": hashtags if isinstance(hashtags, list) else None,
            "statistics": {"comments": integer(row.get("comment_num")), "charges": integer(row.get("link_award_num")),
                           "favourites": integer(row.get("favour_count")), "views": integer(row.get("click"))},
            "media": images, "has_video": truth(row.get("has_video"))}


def document(link, evidence):
    post = summary(link)
    value = link.get("text")
    blocks, gaps = [], []
    status = "available"
    if isinstance(value, str) and (re.match(r"^\s*\[\s*\{", value) or value.strip() == "[]"):
        original = value
        try:
            decoded = json.loads(value)
            if isinstance(decoded, list) and (not decoded or any(isinstance(node, dict) and any(key in node for key in ("type", "text", "url")) for node in decoded)):
                value = decoded
        except ValueError:
            value = None
            gaps.append("invalid_body_json")
            status = "invalid"
            blocks.append({"type": "unknown", "platform_type": "unparsed_body", "data": original, "position": 0})
    if isinstance(value, str) and value:
        value = [{"type": "html", "text": value}]
    if isinstance(value, list):
        for position, node in enumerate(value):
            if not isinstance(node, dict):
                gaps.append("invalid_body_block")
                continue
            kind = node.get("type")
            if kind in ("img", "image"):
                asset = media(node, position)
                if asset:
                    blocks.append({"type": "image", **asset})
                else:
                    gaps.append("image_url_missing")
                    blocks.append({"type": "unknown", "platform_type": kind, "position": position, "data": node})
            elif kind in ("video", "audio"):
                asset = media(node, position)
                if asset:
                    blocks.append({"type": kind, **asset, "data": node})
                else:
                    blocks.append({"type": "unknown", "platform_type": kind, "position": position, "data": node})
                    gaps.append("playable_url_missing")
            elif kind in ("text", "html", None):
                markup = str(node.get("text") or "")
                parsed = Markup()
                parsed.feed(markup)
                parsed.flush()
                for event in parsed.events:
                    if event["type"] == "image":
                        asset = media(event, position)
                        if asset:
                            blocks.append({"type": "image", **asset})
                        else:
                            gaps.append("image_url_missing")
                    else:
                        blocks.append({**event, "position": position})
            else:
                blocks.append({"type": "unknown", "platform_type": kind, "position": position, "data": node})
                gaps.append("unsupported_body_block")
    elif value is not None and value != "":
        gaps.append("invalid_body_shape")
        status = "invalid"
    pieces, previous_position = [], None
    for block in blocks:
        position = block.get("position")
        if previous_position is not None and position != previous_position and pieces and not pieces[-1].endswith("\n"):
            pieces.append("\n")
        if block["type"] == "text":
            pieces.append(block["text"])
        elif block["type"] in ("image", "video", "audio"):
            pieces.append("\n")
        previous_position = position
    text = "".join(pieces).strip()
    if not blocks and status != "invalid":
        status = "missing"
        gaps.append("body_not_returned")
    partial = truth(link.get("is_summary")) is True or truth(link.get("has_more_text")) is True
    full = truth(link.get("is_full_text")) is True
    completeness = "unknown" if status in ("missing", "invalid") else "partial" if partial or gaps else "complete" if full else "unknown"
    post.update(body={"status": status, "completeness": completeness, "text": text, "blocks": blocks,
                      "gaps": sorted(set(gaps)), "source_field": "link.text", "excerpt_is_body": False}, source=evidence)
    body_media = [{**block, "origin": "body"} for block in blocks if block["type"] in ("image", "video", "audio")]
    post["media"] = merge_media(body_media, [{"type": "image", **asset, "origin": "link.imgs"} for asset in post["media"]])
    post["content_sha256"] = hashlib.sha256(json.dumps({k: post[k] for k in ("id", "title", "body", "media")},
                                             sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return post


def comment(raw, post_id, root_id):
    if not isinstance(raw, dict):
        raise XhhConfigError("comment node must be an object")
    uid = identifier(raw.get("commentid") or raw.get("comment_id") or raw.get("id"))
    if not uid:
        raise XhhConfigError("comment record has no valid ID")
    assets = raw.get("imgs") or raw.get("images") or []
    if not isinstance(assets, list):
        assets = [assets]
    parsed = Markup()
    parsed.feed(str(raw.get("text") or ""))
    parsed.flush()
    inline = [asset for asset in (media(event, index) for index, event in enumerate(parsed.events)
                                 if event["type"] == "image") if asset]
    explicit = [asset for asset in (media(item) for item in assets) if asset]
    text = raw.get("text")
    return {"id": uid, "post_id": post_id, "root_id": root_id,
            "reply_to_id": identifier(raw.get("replyid") or raw.get("reply_id") or raw.get("reply_to_id")) if uid != root_id else None,
            "reply_to_uid": identifier(raw.get("replyuserid") or raw.get("reply_userid") or raw.get("reply_to_uid")), "author": author(raw),
            "text": plain(text) if text is not None else None,
            "raw_text": text, "updated_at": integer(raw.get("modify_at") or raw.get("update_at")),
            "observed_fields": ["media"] if any(key in raw for key in ("imgs", "images")) else [],
            "created_at": integer(raw.get("create_at")), "children_count": integer(raw.get("child_num")),
            "has_more": truth(raw.get("has_more")), "statistics": {"up": integer(raw.get("up"))},
            "media": merge_media(inline, explicit)}


def floors(rows, post_id):
    if not isinstance(rows, list):
        raise XhhConfigError("comment floors missing from platform response")
    result = []
    for floor in rows:
        nodes = floor.get("comment") if isinstance(floor, dict) else None
        if not isinstance(nodes, list) or not nodes:
            raise XhhConfigError("invalid comment floor")
        if not all(isinstance(node, dict) for node in nodes):
            raise XhhConfigError("comment floor contains a non-object node")
        root = identifier(nodes[0].get("commentid") or nodes[0].get("comment_id") or nodes[0].get("id"))
        for node in nodes:
            result.append(comment(node, post_id, root))
    return result


def result_object(raw):
    value = raw.get("result") if isinstance(raw, dict) else None
    if not isinstance(value, dict):
        raise XhhConfigError("platform result must be an object")
    return value
