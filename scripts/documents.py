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
            "observed_at": time.time(), "account_uid": str(client.config.heybox_id)}


class Markup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.images = [], [], []
        self.events, self.chunk = [], []

    def flush(self):
        text = "".join(self.chunk).strip()
        if text:
            self.events.append({"type": "text", "text": text})
        self.chunk = []

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag in {"br", "p", "div", "li", "blockquote"}:
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
        if tag not in {"a", "b", "i", "u", "em", "strong", "p", "div", "span", "img", "font", "color", "size",
                       "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "code", "pre",
                       "s", "strike", "del", "ins", "table", "thead", "tbody", "tr", "td", "th", "hr", "br"}:
            self.parts.append(self.get_starttag_text())
            self.chunk.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag in {"p", "div", "li", "blockquote", "pre"}:
            self.parts.append("\n")
            self.chunk.append("\n")

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
    user = raw.get("user") if isinstance(raw.get("user"), dict) else raw
    return {"uid": identifier(raw.get("userid") or user.get("userid") or user.get("id")),
            "name": user.get("username") or user.get("nickname") or None,
            "avatar_url": user.get("avatar") or user.get("avartar") or None}


def media(raw, position=None):
    if isinstance(raw, str):
        raw = {"url": raw}
    if not isinstance(raw, dict):
        return None
    url = raw.get("url") or raw.get("src") or raw.get("img_url")
    if not isinstance(url, str) or not url.startswith("https://"):
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
    communities = [{"id": identifier(topic.get("topic_id") or topic.get("id")),
                    "name": topic.get("name") or topic.get("topic_name")}
                   for topic in row.get("topics", []) if isinstance(topic, dict)]
    article = truth(row.get("is_article"))
    kind = "article" if article is True or str(row.get("post_type")) == "3" else "dynamic" if article is False or str(row.get("link_tag")) == "27" else "unknown"
    raw_images = row.get("imgs") if isinstance(row.get("imgs"), list) else []
    images = [asset for asset in (media(item) for item in raw_images) if asset]
    return {"id": uid, "url": "https://www.xiaoheihe.cn/app/bbs/link/" + uid, "title": plain(row.get("title")),
            "excerpt": plain(row.get("description")), "content_type": kind, "author": author(row),
            "created_at": integer(row.get("create_at")), "updated_at": integer(row.get("modify_at")),
            "communities": communities, "hashtags": row.get("hashtags") if isinstance(row.get("hashtags"), list) else [],
            "statistics": {"comments": integer(row.get("comment_num")), "charges": integer(row.get("link_award_num")),
                           "favourites": integer(row.get("favour_count")), "views": integer(row.get("click"))},
            "media": images, "has_video": truth(row.get("has_video"))}


def document(link, evidence):
    post = summary(link)
    value = link.get("text")
    blocks, gaps = [], []
    status = "available"
    if isinstance(value, str) and value.lstrip().startswith("["):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
            gaps.append("invalid_body_json")
            status = "invalid"
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
            elif kind in ("video", "audio"):
                blocks.append({"type": kind, "url": node.get("url"), "position": position})
            elif kind in ("text", "html", None):
                markup = str(node.get("text") or "")
                if markup.startswith(("/storage/", "/data/", "/sdcard/")):
                    gaps.append("local_body_path")
                    continue
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
                blocks.append({"type": "unknown", "platform_type": kind, "position": position})
                gaps.append("unsupported_body_block")
    elif value is not None and value != "":
        gaps.append("invalid_body_shape")
        status = "invalid"
    text = "\n".join(block["text"] for block in blocks if block["type"] == "text")
    if not blocks and status != "invalid":
        status = "missing"
        gaps.append("body_not_returned")
    partial = truth(link.get("is_summary")) is True or truth(link.get("has_more_text")) is True
    full = truth(link.get("is_full_text")) is True
    completeness = "partial" if partial or gaps else "complete" if full else "unknown"
    post.update(body={"status": status, "completeness": completeness, "text": text, "blocks": blocks,
                      "gaps": sorted(set(gaps)), "source_field": "link.text", "excerpt_is_body": False}, source=evidence)
    post["media"] = [block for block in blocks if block["type"] in ("image", "video", "audio")]
    post["content_sha256"] = hashlib.sha256(json.dumps({k: post[k] for k in ("id", "title", "body")},
                                             sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return post


def comment(raw, post_id, root_id):
    uid = identifier(raw.get("commentid") or raw.get("comment_id") or raw.get("id"))
    if not uid:
        raise XhhConfigError("comment record has no valid ID")
    assets = raw.get("imgs") or raw.get("images") or []
    if not isinstance(assets, list):
        assets = [assets]
    return {"id": uid, "post_id": post_id, "root_id": root_id,
            "reply_to_id": identifier(raw.get("replyid") or raw.get("reply_id")) if uid != root_id else None,
            "reply_to_uid": identifier(raw.get("replyuserid")), "author": author(raw), "text": plain(raw.get("text")),
            "created_at": integer(raw.get("create_at")), "children_count": integer(raw.get("child_num")),
            "has_more": truth(raw.get("has_more")), "statistics": {"up": integer(raw.get("up"))},
            "media": [asset for asset in (media(item) for item in assets) if asset]}


def floors(rows, post_id):
    if not isinstance(rows, list):
        raise XhhConfigError("comment floors missing from platform response")
    result = []
    for floor in rows:
        nodes = floor.get("comment") if isinstance(floor, dict) else None
        if not isinstance(nodes, list) or not nodes:
            raise XhhConfigError("invalid comment floor")
        root = identifier(nodes[0].get("commentid") or nodes[0].get("comment_id") or nodes[0].get("id"))
        for node in nodes:
            result.append(comment(node, post_id, root))
    return result


def result_object(raw):
    value = raw.get("result") if isinstance(raw, dict) else None
    if not isinstance(value, dict):
        raise XhhConfigError("platform result must be an object")
    return value
