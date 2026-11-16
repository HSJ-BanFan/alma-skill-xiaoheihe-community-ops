from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from typing import Callable

from documents import author, comment, document, floors, identifier, integer, link_id, media, merge_media, result_object, source, summary, truth, entity_id, enrich_observation
from network_io import read_http
from paging import (
    Budget,
    ReadLimit,
    StopKind,
    StopState,
    coverage,
    decode_resume,
    finalize_coverage,
)
from xhh_sdk.exceptions import (
    XhhConfigError,
    XhhError,
)
from xhh_sdk.web_signer import sign_web_request
from emoji_catalog import Catalog

COMMANDS = {
    "community", "topics", "hot", "feed", "search", "post", "comments",
    "replies", "thread", "user", "favourites", "hashtags", "stories",
    "banners", "recommended-users", "games",
}

PUBLIC_TITLE_FALLBACKS = {
    "xiaoheihe",
    "小黑盒",
    "小黑盒 - 玩家的游戏社区",
}
HTML_VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "keygen",
    "link", "meta", "param", "source", "track", "wbr",
})
HTML_BLOCK_SEPARATORS = frozenset({
    "p", "div", "li", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5",
    "h6", "tr",
})


@dataclass(frozen=True)
class ReaderSpec:
    reader: str
    protocol: str
    route: str
    handler: Callable | None = None


READER_SPECS = (
    ReaderSpec("native", "app", "/bbs/app/link/tree"),
    ReaderSpec("website", "web", "/bbs/app/link/tree"),
    ReaderSpec("html", "public", "/app/bbs/link/{post_id}"),
)
READERS = {spec.reader: spec for spec in READER_SPECS}


@dataclass(frozen=True)
class ResponseSpec:
    name: str
    paths: tuple[tuple[str, ...], ...]
    adapter: Callable
    route: str | None = None
    placements: tuple[str, ...] = ()
    empty_gap: str | None = None


def bounded(value):
    number = int(value)
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("must be within 1..100")
    return number


def page_index(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("page index must be positive")
    return number


def post_argument(value):
    try:
        return link_id(value)
    except XhhConfigError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def numeric_argument(value):
    if not identifier(value):
        raise argparse.ArgumentTypeError("ID must be a positive integer")
    return str(value)


def platform_filter(value):
    if not isinstance(value, str) or "=" not in value:
        raise argparse.ArgumentTypeError("must use KEY=VALUE")
    key, selected = value.split("=", 1)
    if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key)
            or not selected or len(selected) > 256
            or any(character in selected for character in "\r\n\x00")):
        raise argparse.ArgumentTypeError("invalid platform filter key or value")
    return key, selected


def platform_parameters(args):
    parameters = {}
    protected = {
        "pkey", "user_pkey", "x_pkey", "heybox_id", "user_heybox_id",
        "x_heybox_id", "_time", "nonce", "hkey", "_rnd", "app",
        "x_app", "client_type", "x_client_type", "version",
        "x_client_version", "imei", "device_info",
    }
    for key, value in getattr(args, "platform_filters", None) or []:
        if key.casefold() in protected:
            raise XhhConfigError("platform filter cannot override protected request fields")
        if key in parameters:
            raise XhhConfigError("duplicate platform filter key")
        parameters[key] = value
    if getattr(args, "time_range", None):
        if "time_range" in parameters:
            raise XhhConfigError("time_range was supplied twice")
        parameters["time_range"] = args.time_range
    if getattr(args, "order", None):
        if "sort_filter" in parameters:
            raise XhhConfigError("sort_filter was supplied twice")
        parameters["sort_filter"] = {
            "recommended": "hot-rank",
            "newest": "create",
            "recent-replies": "reply",
        }.get(args.order, args.order)
    return parameters


def _fit_items(data, args, scope, *, matched):
    limit = getattr(args, "limit", None)
    if not isinstance(data.get("items"), list) or not isinstance(limit, int):
        return False
    selected = data["items"][:limit]
    data["items"] = selected
    scope.update(matched_total=matched, selected=len(selected), limit=limit,
                 limit_reached=matched > limit, truncated=matched > limit)
    return matched > limit


class PublicPageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.article_root = None
        self.in_title = False
        self.title = []
        self.meta = {}
        self.heading_depth = 0
        self.headings = []
        self._heading = []
        self.parts = []
        self.json_ld = []
        self._script = None

    def handle_starttag(self, tag, attributes):
        self._start_tag(tag, dict(attributes), self_closing=False)

    def handle_startendtag(self, tag, attributes):
        self._start_tag(tag, dict(attributes), self_closing=True)

    def _start_tag(self, tag, values, *, self_closing):
        if tag not in HTML_VOID_ELEMENTS and not self_closing:
            self.stack.append(tag)
        if tag in {"article", "main"} and not self_closing:
            if tag == "article" or self.article_root is None:
                self.article_root = len(self.stack)
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            key = values.get("property") or values.get("name")
            content = values.get("content")
            if key and content:
                self.meta[str(key).casefold()] = content
        if tag == "h1" and self.article_root is not None:
            self.heading_depth = len(self.stack)
            self._heading = []
        if tag == "script" and not self_closing:
            kind = str(values.get("type") or "").casefold()
            if kind == "application/ld+json":
                self._script = []
        if (self.article_root is not None
                and tag in HTML_BLOCK_SEPARATORS | {"br"}):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            self.json_ld.append("".join(self._script))
            self._script = None
        if tag == "title":
            self.in_title = False
        if tag == "h1" and self.heading_depth:
            text = re.sub(r"\s+", " ", "".join(self._heading)).strip()
            if text:
                self.headings.append(text)
            self.heading_depth = 0
            self._heading = []
        if (self.article_root is not None
                and tag in HTML_BLOCK_SEPARATORS):
            self.parts.append("\n")
        try:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
        except ValueError:
            return
        closes_article = (
            self.article_root is not None
            and index + 1 == self.article_root
        )
        del self.stack[index:]
        if closes_article:
            self.article_root = None

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        if self.in_title:
            self.title.append(data)
        if self.heading_depth:
            self._heading.append(data)
        if (self.article_root is not None
                and len(self.stack) >= self.article_root):
            self.parts.append(data)


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _public_author(value):
    if isinstance(value, str):
        name = re.sub(r"\s+", " ", value).strip()
        return {"uid": None, "name": name or None, "avatar_url": None}
    if not isinstance(value, dict):
        return None
    identifier_value = value.get("identifier")
    if isinstance(identifier_value, dict):
        identifier_value = (identifier_value.get("value")
                            or identifier_value.get("id"))
    uid = identifier(identifier_value)
    if uid is None:
        url = value.get("url") or value.get("sameAs")
        match = re.search(r"/(?:app/)?(?:user|profile)/(\d+)(?:/|$)",
                          str(url or ""))
        uid = match.group(1) if match else None
    name = value.get("name") or value.get("username")
    name = re.sub(r"\s+", " ", name).strip() if isinstance(name, str) else None
    avatar = value.get("image") or value.get("avatar")
    if isinstance(avatar, dict):
        avatar = avatar.get("url") or avatar.get("contentUrl")
    if not uid and not name and not avatar:
        return None
    return {
        "uid": uid,
        "name": name or None,
        "avatar_url": avatar if isinstance(avatar, str) else None,
    }


def _public_page_metadata(parser):
    nodes = []
    article_body = None
    for script in parser.json_ld:
        try:
            decoded = json.loads(script)
        except ValueError:
            continue
        for node in _walk_json(decoded):
            nodes.append(node)
            candidate = node.get("articleBody")
            if article_body is None and isinstance(candidate, str) and candidate.strip():
                article_body = candidate.strip()
    article_nodes = [
        node for node in nodes
        if any(
            isinstance(kind, str)
            and kind.casefold() in {
                "article", "blogposting", "newsarticle", "socialmediaposting",
            }
            for kind in (
                node.get("@type")
                if isinstance(node.get("@type"), list)
                else [node.get("@type")]
            )
        )
    ]
    title, title_source = None, None
    author, author_source = None, None
    for node in article_nodes:
        candidate = node.get("headline") or node.get("name")
        if title is None and isinstance(candidate, str) and candidate.strip():
            title = re.sub(r"\s+", " ", candidate).strip()
            title_source = "json_ld.headline"
        candidate = _public_author(node.get("author"))
        if author is None and candidate:
            author = candidate
            author_source = "json_ld.author"
    if title is None:
        title = parser.meta.get("og:title") or parser.meta.get("twitter:title")
        if isinstance(title, str) and title.strip():
            title = re.sub(r"\s+", " ", title).strip()
            title_source = "meta.title"
        else:
            title = None
    if title is None and parser.headings:
        title = parser.headings[0]
        title_source = "article.h1"
    if title is None:
        candidate = re.sub(r"\s+", " ", "".join(parser.title)).strip()
        if candidate and candidate.casefold() not in PUBLIC_TITLE_FALLBACKS:
            title, title_source = candidate, "html.title"
    if author is None:
        author = _public_author(parser.meta.get("author"))
        if author:
            author_source = "meta.author"
    return title, title_source, author, author_source, article_body


def register(commands):
    for name in sorted(COMMANDS):
        parser = commands.add_parser(name)
        parser.add_argument("--raw", action="store_true")
        parser.add_argument("--requests", type=bounded, default=10)
        parser.add_argument("--seconds", type=bounded, default=30)
        parser.add_argument("--pages", type=bounded, default=1)
        parser.add_argument("--resume")
        parser.add_argument("--snapshot", help="save this public observation and continuation locally")
        parser.add_argument("--since", help="compare with a same-query snapshot")
        if name == "community":
            parser.add_argument("action", choices=("list", "search", "info", "children"))
            parser.add_argument("target", nargs="?")
        elif name == "topics":
            parser.add_argument("query")
            parser.add_argument("--limit", type=bounded, default=20)
        elif name in ("user", "favourites"):
            parser.add_argument("--offset", type=int, default=0)
            parser.add_argument("--limit", type=bounded, default=20)
            if name == "user":
                parser.add_argument("user_id", type=numeric_argument)
                parser.add_argument("--view", choices=("profile", "posts", "followers", "following", "relations"), default="profile")
            else:
                parser.add_argument("action", choices=("folders", "posts"))
                parser.add_argument("folder_id", nargs="?")
        elif name == "hashtags":
            parser.add_argument("action", choices=("ranking", "search", "links"))
            parser.add_argument("target", nargs="?")
            parser.add_argument("--name")
            parser.add_argument("--offset", type=int, default=0)
            parser.add_argument("--limit", type=bounded, default=20)
        elif name == "games":
            parser.add_argument("action", choices=("search", "recommended", "similar", "info"))
            parser.add_argument("target", nargs="?")
            parser.add_argument("--offset", type=int, default=0)
            parser.add_argument("--limit", type=bounded, default=20)
        elif name in ("stories", "recommended-users"):
            parser.add_argument("--offset", type=int, default=0)
            parser.add_argument("--limit", type=bounded, default=20)
        elif name in ("post", "comments", "thread"):
            parser.add_argument("link_id", type=post_argument)
            parser.add_argument("--page", type=page_index, default=1)
            parser.add_argument("--limit", type=bounded, default=50)
            parser.add_argument("--sort", choices=("hot", "time_aes", "time_desc"), default="hot")
            if name == "post":
                parser.add_argument(
                    "--reader",
                    choices=("auto", *READERS),
                    default="auto",
                )
                parser.add_argument("--section", choices=("all", "body", "metadata"), default="all")
                parser.add_argument("--body-format", choices=("text", "blocks", "both"), default="both")
            if name == "thread":
                parser.add_argument("root_id", type=numeric_argument)
                parser.add_argument("--around", type=numeric_argument)
        elif name == "replies":
            parser.add_argument("root_id", type=numeric_argument)
            parser.add_argument("--post-id", type=post_argument)
            parser.add_argument("--cursor")
            parser.add_argument("--limit", type=bounded, default=20)
        else:
            parser.add_argument("--offset", type=int, default=0)
            parser.add_argument("--limit", type=bounded, default=20)
            if name == "feed":
                parser.add_argument("topic_id", type=numeric_argument)
                parser.add_argument("--sort", choices=("platform", "created", "comments", "charges"), default="platform")
                parser.add_argument("--order", help="platform sort_filter key or recommended/newest/recent-replies")
                parser.add_argument("--time-range", help="platform time_range key returned by navigation")
                parser.add_argument("--platform-filter", dest="platform_filters",
                                    action="append", type=platform_filter, default=[],
                                    metavar="KEY=VALUE", help="forward one platform menu filter")
                parser.add_argument("--content-type", choices=("all", "article", "dynamic"), default="all")
                parser.add_argument("--after", type=int)
                parser.add_argument("--min-comments", type=int, default=0)
            if name == "search":
                parser.add_argument("query", nargs="?")
                parser.add_argument("--type", choices=("link", "game", "user"), default="link")
                parser.add_argument("--hot-words", action="store_true")
                parser.add_argument("--suggest")
                parser.add_argument("--community")
                parser.add_argument("--order", help="platform sort_filter key")
                parser.add_argument("--time-range", help="platform time_range key")
                parser.add_argument("--platform-filter", dest="platform_filters",
                                    action="append", type=platform_filter, default=[],
                                    metavar="KEY=VALUE")
                parser.add_argument("--content-type", choices=("all", "article", "dynamic"), default="all")
                parser.add_argument("--after", type=int)
                parser.add_argument("--min-comments", type=int, default=0)
                parser.add_argument("--sort", choices=("platform", "created", "comments", "charges"), default="platform")


def rows(result, keys):
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in keys:
            if isinstance(result.get(key), list):
                return result[key]
    raise XhhConfigError("expected list not present in platform result")


def first_rows(result, keys, *, required=True):
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in keys:
            if isinstance(result.get(key), list):
                return result[key]
    if required:
        raise XhhConfigError("expected list not present in platform result")
    return []


def hashtag(raw):
    if not isinstance(raw, dict):
        raise XhhConfigError("hashtag record must be an object")
    uid = identifier(raw.get("hashtag_id") or raw.get("tag_id")
                     or raw.get("topic_id") or raw.get("id"))
    name = raw.get("name") or raw.get("hashtag_name") or raw.get("tag_name")
    if not uid:
        raise XhhConfigError("hashtag record has no valid ID")
    return {
        "id": uid,
        "name": name,
        "description": raw.get("description") or raw.get("desc"),
        "icon_url": raw.get("icon") or raw.get("pic_url") or raw.get("avatar"),
        "stats": {
            "links": integer(raw.get("link_num") or raw.get("post_num")),
            "followers": integer(raw.get("follow_num") or raw.get("fans_num")),
            "heat": integer(raw.get("heat") or raw.get("hot_num")),
        },
    }


def game(raw):
    if not isinstance(raw, dict):
        raise XhhConfigError("game record must be an object")
    uid = identifier(raw.get("appid") or raw.get("app_id") or raw.get("game_id")
                     or raw.get("gameid") or raw.get("steam_appid") or raw.get("id"))
    if not uid:
        raise XhhConfigError("game record has no valid ID")
    aliases = raw.get("aliases") or raw.get("alias")
    if isinstance(aliases, str):
        aliases = [aliases]
    communities = []
    candidates = raw.get("communities") or raw.get("topics") or raw.get("topic_list")
    if isinstance(candidates, list):
        for topic in candidates:
            if not isinstance(topic, dict):
                continue
            community_id = identifier(topic.get("topic_id") or topic.get("id"))
            if community_id:
                communities.append({
                    "id": community_id,
                    "name": topic.get("name") or topic.get("topic_name"),
                })
    topic_id = identifier(raw.get("topic_id") or raw.get("topicid"))
    if topic_id and not any(item["id"] == topic_id for item in communities):
        communities.append({"id": topic_id, "name": raw.get("topic_name")})
    return {
        "id": uid,
        "name": raw.get("name") or raw.get("game_name") or raw.get("title"),
        "aliases": aliases if isinstance(aliases, list) else None,
        "description": raw.get("description") or raw.get("desc") or raw.get("intro"),
        "icon_url": raw.get("icon") or raw.get("appicon") or raw.get("icon_url"),
        "cover_url": (raw.get("cover") or raw.get("cover_url") or raw.get("image")
                      or raw.get("library_background")
                      or raw.get("libary_background")),
        "platforms": (raw.get("platforms") or raw.get("platform")
                      or [item.get("key") for item in raw.get("platform_infos") or []
                          if isinstance(item, dict) and item.get("key")]),
        "tags": raw.get("tags") or raw.get("game_tags") or raw.get("genres")
                or raw.get("rich_tags"),
        "price": raw.get("price") if isinstance(raw.get("price"), dict) else None,
        "is_free": truth(raw.get("is_free")),
        "chinese_support": truth(raw.get("chinese_support")),
        "followers": integer(raw.get("follow_num")),
        "score": raw.get("score"),
        "score_comment": raw.get("score_comment"),
        "communities": communities or None,
        "navigation": {
            "feed": ({"command": "feed", "topic_id": communities[0]["id"]}
                     if communities else None),
            "similar_games": {"command": "games similar", "game_id": uid},
        },
    }


GAME_RELATION_GROUPS = ("morelike", "similar_games", "publisher_games")


def game_detail(result, target, evidence):
    identities = [identifier(result[key]) for key in ("steam_appid", "appid")
                  if key in result]
    if not identities or any(identity != target for identity in identities):
        raise XhhConfigError("returned game identity does not match requested Steam app ID")
    detail = game(result)
    detail["steam_app_id"] = identifier(result.get("steam_appid"))
    detail["description"] = detail["description"] or result.get("about_the_game")
    gaps, invalid = [], 0
    topic = result.get("topic_detail")
    community = None
    if isinstance(topic, dict) and identifier(topic.get("topic_id")):
        nested_game = topic.get("game")
        app_ids = nested_game.get("app_id") if isinstance(nested_game, dict) else None
        app_ids = app_ids if isinstance(app_ids, list) else [app_ids]
        forum = truth(result.get("has_bbs", topic.get("has_bbs")))
        community = {
            "id": identifier(topic["topic_id"]),
            "name": topic.get("name") or topic.get("topic_name"),
            "icon_url": (topic.get("icon_url") or topic.get("icon")
                         or topic.get("pic_url") or topic.get("small_pic_url")),
            "forum_available": forum,
            "game_ids": list(dict.fromkeys(uid for value in app_ids
                                           if (uid := identifier(value)))),
            "navigation": {"feed": {"command": "feed", "topic_id": identifier(topic["topic_id"])}
                           if forum is True else None},
        }
    else:
        gaps.append("community_not_returned" if "topic_detail" not in result
                    else "invalid_community")
        if "topic_detail" in result:
            invalid += 1
    detail["navigation"] = {
        "info": {"command": "games info", "steam_app_id": target},
        "feed": community["navigation"]["feed"] if community else None,
        "similar_games": {"command": "games similar", "game_id": detail["id"]},
    }
    related, groups = {}, {}
    for key in GAME_RELATION_GROUPS:
        if key not in result:
            groups[key] = {"status": "not_returned", "returned": None,
                           "valid": None, "invalid": None}
            gaps.append(f"{key}_not_returned")
            continue
        records = result[key]
        if not isinstance(records, list):
            groups[key] = {"status": "observed_with_invalid", "returned": 0,
                           "valid": 0, "invalid": 1}
            gaps.append(f"invalid_{key}_shape")
            invalid += 1
            continue
        rejected = 0
        for record in records:
            try:
                item = game(record)
            except (XhhConfigError, TypeError, ValueError):
                rejected += 1
                continue
            uid = item["id"]
            if uid in related:
                memberships = related[uid]["relations"]
                item = enrich_observation(related[uid], {
                    field: value for field, value in item.items()
                    if value is not None and value != []
                })
            else:
                memberships = []
            item["relations"] = memberships + ([key] if key not in memberships else [])
            related[uid] = item
        groups[key] = {
            "status": ("observed_with_invalid" if rejected else
                       "observed" if records else "observed_empty"),
            "returned": len(records), "valid": len(records) - rejected,
            "invalid": rejected,
        }
        invalid += rejected
        if rejected:
            gaps.append(f"invalid_{key}_records")
    menus = []
    menu_entries = result.get("menu_v2", [])
    if not isinstance(menu_entries, list):
        gaps.append("invalid_menu_v2_shape")
        invalid += 1
        menu_entries = []
    for entry in menu_entries:
        if isinstance(entry, dict):
            menus.append({
                "key": entry.get("key") or entry.get("uidsl_key"),
                "title": entry.get("title") or entry.get("name"),
                "type": entry.get("type"),
                "protocol": entry.get("protocol") or entry.get("prot"),
                "value": entry.get("value"),
                "parameters": next((
                    entry.get(key) for key in ("uidsl_params", "parameters", "params")
                    if entry.get(key) is not None
                ), None),
            })
        else:
            if "invalid_menu_v2_records" not in gaps:
                gaps.append("invalid_menu_v2_records")
            invalid += 1
    return {
        "game": detail, "community": community,
        "related_games": list(related.values()), "relation_groups": groups,
        "menus": menus, "source": evidence,
    }, {"completion": "complete", "full_history": False, "gaps": gaps,
        "invalid": invalid, "excluded": invalid}


def discovery_user(raw, catalog=None):
    user = author(raw)
    if not user["uid"]:
        raise XhhConfigError("user record has no valid ID")
    return user


def discovery_game(raw, catalog=None):
    return game(raw.get("game", raw))


def search_payload(raw, expected):
    kind = raw.get("type")
    info = raw.get("info")
    if kind is not None and kind != expected:
        raise XhhConfigError(f"search returned {kind!r} where {expected!r} was required")
    if isinstance(info, dict):
        return info
    if kind is not None:
        raise XhhConfigError(f"search {expected} result has no object payload")
    return raw


def search_link(raw, catalog=None):
    return summary(search_payload(raw, "link"), catalog)


def search_game(raw, catalog=None):
    return discovery_game(search_payload(raw, "game"))


def search_user(raw, catalog=None):
    return discovery_user(search_payload(raw, "user"))


def story(raw, catalog=None):
    if "link_card_info" not in raw:
        post = summary(raw, catalog)
        post.update(type="story", provenance={"identity": "link_id", "shape": "link"})
        return post
    card, identity = raw["link_card_info"], raw.get("card_id_info")
    uid = identifier(identity.get("item_id")) if isinstance(identity, dict) else None
    if not isinstance(card, dict) or not uid:
        raise XhhConfigError("story card has no valid item ID or link content")
    fields = {key: card.get(key) for key in (
        "title", "is_article", "post_type", "link_tag", "userid",
        "create_at", "modify_at", "topics", "imgs", "has_video",
        "comment_num", "link_award_num", "favour_count", "click",
    )}
    post = summary({
        **fields,
        "linkid": uid,
        "description": card.get("description") or card.get("desc") or card.get("excerpt"),
        "user": card.get("user") or card.get("user_info"),
    }, catalog)
    stats = card.get("stat") or card.get("stats") or card.get("statistics")
    if isinstance(stats, dict):
        for name, key in (("comments", "comment_num"), ("charges", "link_award_num"),
                          ("favourites", "favour_count"), ("views", "click")):
            if post["statistics"][name] is None:
                post["statistics"][name] = integer(stats.get(key, stats.get(name)))
    video = card.get("video_info") or card.get("video")
    video = video if isinstance(video, dict) else {}
    cover = media(card.get("cover") or card.get("cover_url") or card.get("thumb")
                  or video.get("cover") or video.get("cover_url"))
    playable = media({**video, "url": video.get("url") or video.get("video_url")})
    post.update(
        type="story",
        description=post["excerpt"],
        cover_url=cover["url"] if cover else None,
        video={**playable, "duration": integer(video.get("duration"))} if playable else None,
        provenance={"identity": "card_id_info.item_id", "shape": "link_card_info",
                    "card_type": raw.get("card_type")
                    if isinstance(raw.get("card_type"), (str, int)) else None},
    )
    if post["has_video"] is None and video:
        post["has_video"] = True
    return post


def banner(raw, catalog=None):
    topic = raw.get("topic_info") or raw.get("topic") or raw
    if not isinstance(topic, dict):
        raise XhhConfigError("banner community must be an object")
    raw_uid = topic.get("topic_id") or topic.get("topicid") or topic.get("id")
    placement = raw.get("placement")
    protocol = topic.get("protocol") if isinstance(topic.get("protocol"), str) else None
    icon_url = (topic.get("small_pic_url") or topic.get("pic_url")
                or topic.get("icon"))
    if str(raw_uid) == "-1":
        name = topic.get("name") or topic.get("topic_name")
        return {"id": f"{placement}:all" if placement else "all",
                "name": name, "community": None, "url": None,
                "platform_protocol": protocol, "placement": placement,
                "description": topic.get("description") or topic.get("desc"),
                "icon_url": icon_url}
    uid = identifier(raw_uid)
    if not uid:
        raise XhhConfigError("banner community has no valid ID")
    name = topic.get("name") or topic.get("topic_name")
    return {"id": f"{placement}:{uid}" if placement else uid,
            "name": name, "community": {"id": uid, "name": name},
            "url": "https://www.xiaoheihe.cn/app/bbs/topic/" + uid,
            "platform_protocol": protocol,
            "placement": placement,
            "description": topic.get("description") or topic.get("desc"),
            "icon_url": icon_url}


RESPONSES = {
    "stories": ResponseSpec("story", (("links",), ("items",), ("list",), ("stories",)),
                            story, route="/bbs/app/story_mode/list"),
    "banners": ResponseSpec("banner", (("topic_banner", "top_topics"),
                                     ("topic_banner", "subscribed_topics"),
                                     ("banners",), ("items",), ("list",)),
                            banner, route="/bbs/app/feeds/banner", placements=("top", "subscribed")),
    "recommended-users": ResponseSpec("user", (("recommend_list",), ("rec_users",),
                                             ("users",), ("items",), ("recommend",), ("list",)),
                                      discovery_user, route="/bbs/app/profile/recommend/following",
                                      empty_gap="recommendations_empty"),
    "link": ResponseSpec("link", (("links",), ("search_result",), ("items",), ("list",), ("news",)), search_link),
    "feed": ResponseSpec("link", (("links",), ("items",), ("list",), ("news",)), search_link),
    "hashtag-links": ResponseSpec("link", (("links",), ("items",), ("list",)), search_link),
    "game": ResponseSpec("game", (("games",), ("search_result",), ("items",), ("list",),
                                  ("links",), ("news",), ("recommend",), ("similar",)), search_game),
    "user": ResponseSpec("user", (("users",), ("search_result",), ("items",), ("list",), ("links",), ("news",)), search_user),
}


def response_records(result, spec):
    candidates = []
    for path in spec.paths:
        value = result
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        candidates.append(value)
    grouped = candidates[:len(spec.placements)]
    if spec.placements and any(isinstance(value, list) for value in grouped):
        records = [dict(row, placement=placement) if isinstance(row, dict) else row
                   for value, placement in zip(grouped, spec.placements)
                   if isinstance(value, list) for row in value]
    else:
        lists = [value for value in candidates if isinstance(value, list)]
        if not lists:
            raise XhhConfigError("expected list not present in platform result")
        return next((value for value in lists if value), lists[0])
    return records


def normalize_response(result, spec, *, include_occurrences=False, catalog=None):
    records = response_records(result, spec)
    items, invalid = [], 0
    occurrences = []
    for row in records:
        if not isinstance(row, dict):
            invalid += 1
            occurrences.append({
                "content": None,
                "digest": hashlib.sha256(json.dumps(
                    row, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest(),
                "reason": "malformed_record",
            })
            continue
        try:
            item = spec.adapter(row, catalog)
        except XhhConfigError:
            invalid += 1
            occurrences.append({
                "content": None,
                "digest": hashlib.sha256(json.dumps(
                    row, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest(),
                "reason": "malformed_record",
            })
            continue
        items.append(item)
        occurrences.append({
            "content": item,
            "digest": hashlib.sha256(json.dumps(
                row, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode()).hexdigest(),
            "reason": None,
        })
    gaps = [f"invalid_{spec.name}_records"] if invalid else []
    if not records and spec.empty_gap:
        gaps.append(spec.empty_gap)
    value = (items, len(records), invalid, gaps)
    return (*value, occurrences) if include_occurrences else value


def post_filter_reason(args, item):
    if (args.content_type != "all"
            and item["content_type"] != args.content_type):
        return "filter_excluded"
    if (args.after is not None
            and (item["created_at"] is None
                 or item["created_at"] < args.after)):
        return "filter_excluded"
    if ((item["statistics"]["comments"] or 0)
            < args.min_comments):
        return "filter_excluded"
    return None


def communities(value):
    found = {}
    def visit(node, path):
        if isinstance(node, list):
            for child in node:
                visit(child, path)
        elif isinstance(node, dict):
            name = node.get("name") or node.get("topic_name")
            uid = identifier(node.get("topic_id"))
            if uid:
                found[uid] = {"id": uid, "name": name, "description": node.get("description") or node.get("desc"),
                              "url": "https://www.xiaoheihe.cn/app/bbs/topic/" + uid, "category_path": path,
                              "icon_url": node.get("pic_url") or node.get("icon"),
                              "subscribed": node.get("is_follow")}
            next_path = path + [name] if name and not uid else path
            for child in node.values():
                if isinstance(child, (list, dict)):
                    visit(child, next_path)
    visit(value, [])
    return list(found.values())


def query_key(args):
    return hashlib.sha256(json.dumps({key: value for key, value in vars(args).items()
                if key not in {"snapshot", "since", "resume", "pages", "requests", "seconds", "raw", "page", "offset", "cursor",
                               "account", "config", "env", "protocol", "data_dir"}}, sort_keys=True, default=str).encode()).hexdigest()


def request_arguments(args):
    holder = argparse.ArgumentParser(add_help=False)
    commands = holder.add_subparsers(dest="command")
    register(commands)
    result = [args.command]
    for action in commands.choices[args.command]._actions:
        if action.dest in {"help", "snapshot", "since", "resume", "raw"}:
            continue
        value = getattr(args, action.dest, None)
        if value is None:
            continue
        if action.dest == "platform_filters":
            for key, selected in value:
                result.extend([
                    action.option_strings[0],
                    f"{key}={selected}",
                ])
            continue
        if action.option_strings:
            if action.nargs == 0:
                if value:
                    result.append(action.option_strings[0])
            else:
                result.extend([action.option_strings[0], str(value)])
        else:
            result.append(str(value))
    return result


class Browser:
    def __init__(self, client, *, clients=None, capture=None):
        self.client = client
        self.clients = dict(clients or {client.config.protocol_mode: client})
        self.verified_client_ids = set()
        self.identity_verified = False
        self.catalog = None
        self.capture = capture
        self.capture_results = []

    def record(self, nodes, evidence, post_id, root_id, scope):
        if self.capture is not None:
            result = self.capture(nodes, evidence, post_id, root_id, scope)
            self.capture_results.append(result)

    def _record_comment_floors(
            self, result, evidence, post_id, root_id, *, position, mode,
            scope_updates=None):
        records = result.get("comments")
        nodes = floors(records, post_id, self.catalog)
        scope = coverage(
            result,
            count=len(records),
            position=position,
            mode=mode,
        )
        if scope_updates:
            scope.update(scope_updates)
        self.record(nodes, evidence, post_id, root_id, scope)
        return nodes, scope

    def capture_coverage(self, scope):
        if self.capture_results:
            scope["interaction_capture"] = {
                "pages": self.capture_results,
                "complete": all(
                    result.get("persisted")
                    or result.get("reason") == "no_trusted_uid_namespace"
                    for result in self.capture_results
                ),
            }

    @staticmethod
    def _session_revision(client):
        extra = getattr(client.config, "extra", None)
        return (extra or {}).get("session_revision")

    def fetch(self, route, query, budget, *, client=None):
        selected = client or self.client
        raw = budget.call(selected, lambda: selected.transport.signed_request(route, query=query))
        try:
            if not isinstance(raw, dict):
                raise XhhConfigError("platform response must be an object")
            if (route in ("/bbs/app/profile/follower/list", "/bbs/app/profile/following/list")
                    and "follow_list" in raw):
                result = raw
            elif route == "/bbs/app/profile/user/link/list" and isinstance(raw.get("post_links"), list):
                result = {"links": raw["post_links"], "user": raw.get("user"), "has_more": raw.get("has_more")}
            else:
                result = result_object(raw)
        except XhhError as error:
            error.raw_response = raw
            raise
        evidence = source(selected, route)
        verified = id(selected) in self.verified_client_ids
        evidence["identity_verified"] = verified
        evidence["identity_verified_this_read"] = verified
        evidence["session_revision"] = self._session_revision(selected)
        return raw, result, evidence

    def execute(self, args, *, verify_account=False, prior_data=None):
        self.catalog = Catalog(data_dir=getattr(args, "data_dir", None))
        budget = Budget(
            args.requests,
            args.seconds,
            pages=args.pages,
            items=getattr(args, "limit", 100),
        )
        post_plan = self._reader_plan(args) if args.command == "post" else None
        if verify_account and not (post_plan and all(
                spec.protocol == "public" for spec in post_plan)):
            from operations import verify_identity
            if args.requests < 2:
                raise XhhConfigError("saving, resuming, or comparing observations requires at least two requests including account verification")
            budget.call(self.client, lambda: verify_identity(self.client))
            self.verified_client_ids.add(id(self.client))
            self.identity_verified = True
            identity_source = source(self.client, "/account/restore_login")
            identity_source.update(identity_verified=True, identity_verified_this_read=True)
            self.record([], identity_source, None, None, {"scope": "identity_check", "completion": "unknown"})
        if args.command == "post":
            return self.post(
                args, budget, plan=post_plan, prior_data=prior_data)
        if args.command == "thread":
            return self.thread(args, budget, prior_data)
        paged = (args.command in ("comments", "replies", "feed", "hot", "stories",
                                  "recommended-users")
                 or args.command == "search" and not (
                     args.hot_words or args.suggest)
                 or args.command == "user" and args.view in ("posts", "followers", "following")
                 or args.command == "hashtags" and args.action == "links"
                 or args.command == "games" and args.action != "info"
                 or args.command == "favourites" and args.action == "posts")
        if not paged and (args.pages != 1 or args.resume):
            raise XhhConfigError("this command returns one resource; pages/resume are not applicable")
        mode = "page" if args.command == "comments" else "cursor" if args.command == "replies" else "offset"
        position_key = {"page": "page", "cursor": "cursor", "offset": "offset"}[mode]
        binding = hashlib.sha256(json.dumps({key: value for key, value in vars(args).items()
                    if key not in {"resume", "pages", "requests", "seconds", "raw", "snapshot", "since", position_key,
                                   "account", "config", "env", "protocol", "data_dir"}},
                    sort_keys=True, default=str).encode()).hexdigest()
        item_window = None
        if args.resume:
            token = decode_resume(args.resume, args.command, self.client.config.heybox_id)
            required = {"binding", "position", "protocol"}
            if (not required <= set(token)
                    or set(token) - required - {"item_window"}
                    or token["binding"] != binding
                    or token["protocol"] != self.client.config.protocol_mode):
                raise XhhConfigError("resume does not match the requested resource or filters")
            value = token["position"]
            if mode == "cursor":
                if (value is not None
                        and (not isinstance(value, str) or len(value) > 4096)):
                    raise XhhConfigError("invalid continuation cursor")
                if value is None and "item_window" not in token:
                    raise XhhConfigError("invalid continuation cursor")
            elif type(value) is not int or value < (1 if mode == "page" else 0):
                raise XhhConfigError("invalid continuation position")
            if "item_window" in token:
                item_window = token["item_window"]
                if (not isinstance(item_window, dict)
                        or set(item_window) != {"position", "consumed"}
                        or item_window["position"] != value
                        or not isinstance(item_window["consumed"], list)
                        or len(item_window["consumed"]) > 10000
                        or not all(
                            isinstance(key, str) and 0 < len(key) <= 256
                            for key in item_window["consumed"]
                        )):
                    raise XhhConfigError(
                        "item continuation is invalid or belongs to another position")
            setattr(args, position_key, value)
        items, identities, positions, raw_pages = [], set(), set(), []
        observed_positions = []
        data, scope = None, {"completion": "unknown", "gaps": [], "next": getattr(args, position_key, None)}
        duplicates = 0
        identity_positions = {}
        returned_total, excluded_total = 0, 0
        invalid_total, normalization_gaps = 0, set()
        matched_total = 0
        stop = None
        resume_parameters = None
        skipped = set(item_window["consumed"] if item_window else [])
        next_position = getattr(args, position_key, None)
        for _ in range(args.pages):
            if paged:
                try:
                    budget.reserve_page()
                except ReadLimit as error:
                    stop = StopState.budget_pause(error.reason)
                    if next_position is not None:
                        resume_parameters = {"position": next_position}
                    break
            position = getattr(args, position_key, None)
            if position in positions and paged:
                stop = StopState.cursor_stalled()
                scope["gaps"].append("repeated_continuation")
                break
            positions.add(position)
            try:
                page, current = self.page(args, budget)
            except ReadLimit as error:
                stop = StopState.budget_pause(error.reason)
                if paged and next_position is not None:
                    resume_parameters = {"position": next_position}
                break
            except XhhError as error:
                if data is None:
                    raise
                stop = StopState.from_error(error)
                if (stop.kind == StopKind.RETRYABLE_FAILURE
                        and next_position is not None):
                    resume_parameters = {"position": next_position}
                break
            data = page
            if paged:
                observed_positions.append(position)
            if args.raw and "raw" in page:
                raw_pages.append(page.pop("raw"))
            scope = current
            next_position = current.get("next")
            if paged and mode == "cursor" and next_position is not None and next_position == position:
                stop = StopState.cursor_stalled()
                scope["gaps"].append("repeated_continuation")
                scope["next"] = None
                next_position = None
            returned_total += current.get("returned", 0)
            excluded_total += current.get("excluded", 0)
            invalid_total += current.get("invalid", 0)
            normalization_gaps.update(current.get("gaps", []))
            if not paged:
                break
            page_keys = []
            page_matched = 0
            current_page_keys = []
            unsafe_remainder = False
            for item in page["items"]:
                key = self._item_window_key(args, item)
                identity = entity_id(item) if isinstance(item, dict) else None
                if key is not None and key in skipped:
                    continue
                if identity and identity in identities:
                    duplicates += 1
                    index = identity_positions[identity]
                    items[index] = enrich_observation(items[index], item)
                    continue
                page_matched += 1
                page_keys.append(key)
                if len(items) >= budget.item_limit:
                    if key is None:
                        unsafe_remainder = True
                    continue
                if identity:
                    identities.add(identity)
                    identity_positions[identity] = len(items)
                items.append(item)
                current_page_keys.append(key)
                if key is None:
                    unsafe_remainder = True
            matched_total += page_matched
            if page_matched > len(current_page_keys):
                if unsafe_remainder or any(key is None for key in page_keys):
                    public = XhhConfigError(
                        "item limit stopped inside a page that has no stable record keys",
                        code="unsafe_item_continuation",
                        fix="increase --limit or request a normalized command with stable IDs",
                    ).public()
                    stop = StopState(
                        StopKind.FATAL,
                        reason="unsafe_item_continuation",
                        error=public,
                    )
                else:
                    consumed = sorted(skipped | set(current_page_keys))
                    resume_parameters = {
                        "position": position,
                        "item_window": {
                            "position": position,
                            "consumed": consumed,
                        },
                    }
                    stop = StopState.budget_pause("item_budget")
                break
            if len(items) >= budget.item_limit:
                if current["completion"] == "complete":
                    stop = StopState.complete()
                elif next_position is not None:
                    stop = StopState.budget_pause("item_budget")
                    resume_parameters = {"position": next_position}
                else:
                    public = XhhConfigError(
                        "output limit reached without a safe continuation",
                        code="unsafe_item_continuation",
                        fix="increase --limit or correct the platform continuation",
                    ).public()
                    stop = StopState(
                        StopKind.FATAL,
                        reason="unsafe_item_continuation",
                        error=public,
                    )
                break
            if stop is not None:
                break
            if current["completion"] == "complete" or next_position is None:
                if current["completion"] == "complete":
                    stop = StopState.complete()
                elif current["completion"] != "complete":
                    stop = StopState.fatal("continuation_missing")
                break
            setattr(args, position_key, next_position)
        if data is None and paged:
            data = {"items": []}
        if paged:
            if (args.command == "feed"
                    or args.command == "search"
                    and getattr(args, "type", "link") == "link"
                    and not (args.hot_words or args.suggest)):
                if args.sort != "platform":
                    items.sort(
                        key=lambda item: item["created_at"] or 0
                        if args.sort == "created"
                        else item["statistics"][args.sort] or 0,
                        reverse=True,
                    )
            data["items"] = items
            scope["duplicates_removed"] = duplicates
            scope["returned_total"] = returned_total
            scope["matched_total"] = matched_total
            scope.update(
                limit=budget.item_limit,
                limit_reached=matched_total > len(items),
                truncated=matched_total > len(items),
            )
            if args.command in ("feed", "search"):
                scope.update(selected=len(items), excluded=excluded_total,
                             matched_total=matched_total)
            else:
                scope.update(selected=len(items), matched_total=matched_total)
            if "invalid" in scope or invalid_total:
                scope["invalid"] = invalid_total
            if "excluded" in scope or excluded_total:
                scope["excluded"] = excluded_total
            scope["gaps"] = sorted(normalization_gaps)
        elif (data is not None and args.command == "user"
              and args.view == "relations"):
            truncated = _fit_items(data, args, scope, matched=len(data["items"]))
            if truncated or data["count"] > len(data["items"]):
                scope["completion"] = "partial"
                scope["gaps"].append("relation_collection_partial")
                scope["missing_count"] = max(0, data["count"] - len(data["items"]))
                stop = StopState(StopKind.DATA_GAP, reason="relation_collection_partial")
        elif (data is not None and isinstance(data.get("items"), list)
              and hasattr(args, "limit")):
            if _fit_items(data, args, scope, matched=len(data["items"])):
                scope["completion"] = "complete"
                scope["gaps"].append("single_response_truncated")
                scope["omitted"] = (
                    scope["matched_total"] - scope["selected"]
                )
                stop = StopState.budget_pause("item_budget")
        if args.raw:
            if data is None and raw_pages:
                data = {"raw_pages": raw_pages}
            elif data is not None:
                data["raw_pages"] = raw_pages
        if stop is None:
            if not paged or scope.get("completion") == "complete":
                stop = StopState.complete()
            elif next_position is not None:
                stop = StopState.budget_pause("page_budget")
                resume_parameters = {"position": next_position}
            else:
                stop = StopState.fatal("continuation_missing")
        if invalid_total and stop.kind == StopKind.COMPLETE:
            stop = StopState(StopKind.DATA_GAP, reason="invalid_records")
        budget.consume_items(len((data or {}).get("items") or []))
        scope["budget"] = budget.view()
        scope["observed_positions"] = observed_positions
        finalize_coverage(
            scope,
            stop,
            command=args.command,
            uid=self.client.config.heybox_id,
            protocol=self.client.config.protocol_mode,
            binding=binding,
            resume_parameters=resume_parameters,
        )
        self.capture_coverage(scope)
        return data, scope

    @staticmethod
    def _item_window_key(args, item):
        identity = entity_id(item) if isinstance(item, dict) else None
        if not identity:
            return None
        entity_type = {
            "comments": "comment",
            "replies": "comment",
            "feed": "post",
            "hot": "post",
            "search": (
                args.type if args.command == "search" else "post"
            ),
            "user": "user",
            "favourites": "post",
            "hashtags": "hashtag",
            "games": "game",
            "stories": "post",
            "recommended-users": "user",
        }.get(args.command, args.command)
        return f"{entity_type}:{identity}"

    def website(self, query, budget, *, client=None):
        selected = client or self.client
        if selected.config.protocol_mode != "web":
            raise XhhConfigError("website reader requires an explicitly selected Web session")
        route = "/bbs/app/link/tree"
        signature = sign_web_request(route)
        parameters = {"client_type": "web", "x_client_type": "web", "os_type": "web", "x_os_type": "Windows", "x_app": "heybox_website",
                      "version": "999.0.4", "web_version": "2.5", "owner_only": "1",
                      "heybox_id": selected.config.heybox_id, **{key: value for key, value in signature.items() if key != "_rnd"}, **query}
        request = urllib.request.Request(selected.config.api_base.rstrip("/") + route + "?" + urllib.parse.urlencode(parameters),
                    headers={"Cookie": selected.config.cookie_web, "Referer": selected.config.web_origin + "/",
                             "User-Agent": selected.config.user_agent_web}, method="GET")
        transport = selected.transport
        raw = budget.call(selected, lambda: transport._checked_response(route, transport._decode(transport._open(request))))
        evidence = source(selected, route)
        verified = id(selected) in self.verified_client_ids
        evidence["identity_verified"] = verified
        evidence["identity_verified_this_read"] = verified
        evidence["session_revision"] = self._session_revision(selected)
        evidence["reader"] = "website"
        return raw, result_object(raw), evidence

    def native(self, query, budget, *, client=None):
        raw, result, evidence = self.fetch(
            "/bbs/app/link/tree", query, budget, client=client)
        evidence["reader"] = "native"
        return raw, result, evidence

    def public_html(self, post_id, budget):
        route = "/app/bbs/link/" + post_id
        url = self.client.config.web_origin.rstrip("/") + route
        request = urllib.request.Request(url, headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": self.client.config.user_agent_web,
        }, method="GET")
        body = budget.call_wire(
            lambda remaining, wire_limit, wire_counter: read_http(
                request,
                max(0.01, remaining),
                profile="public",
                http_timeout=min(self.client.config.timeout, remaining),
                wire_limit=wire_limit,
                wire_counter=wire_counter,
            ),
        )
        try:
            page = body.decode("utf-8")
        except UnicodeDecodeError:
            page = body.decode("utf-8", errors="replace")
        parser = PublicPageParser()
        parser.feed(page)
        title, title_source, page_author, author_source, article_body = (
            _public_page_metadata(parser)
        )
        text = article_body or re.sub(r"[ \t]+", " ", "".join(parser.parts))
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        link = {"id": post_id, "text": text}
        if title is not None:
            link["title"] = title
        if page_author is not None:
            link["user"] = page_author
        evidence = {"route": route, "protocol": "public", "observed_at": time.time(),
                    "configured_uid": str(self.client.config.heybox_id),
                    "identity_verified": False, "reader": "html",
                    "credentials_sent": False, "session_revision": None,
                    "metadata_sources": {
                        key: value for key, value in {
                            "title": title_source,
                            "author": author_source,
                        }.items() if value is not None
                    }}
        data = document(link, evidence)
        metadata_gaps = []
        if title is None:
            metadata_gaps.append("html_title_not_found")
        if page_author is None:
            metadata_gaps.append("html_author_not_found")
        data["metadata_gaps"] = metadata_gaps
        if not text:
            data["body"]["gaps"] = sorted(set(
                data["body"]["gaps"] + ["html_article_not_found"]))
        return data, page

    @staticmethod
    def _reader_score(data):
        body = data.get("body") if isinstance(data, dict) else None
        if not isinstance(body, dict):
            return (-1, 0, 0)
        completeness = {"complete": 3, "available": 2, "partial": 1}.get(
            body.get("completeness"), 2 if body.get("status") == "available" else 0)
        text_length = len(body.get("text") or "")
        structural = len(body.get("blocks") or [])
        return completeness, text_length, structural

    @staticmethod
    def _merge_post_documents(documents, body_document):
        content_keys = {
            "id", "url", "title", "excerpt", "content_type", "author",
            "created_at", "updated_at", "communities", "hashtags",
            "statistics", "has_video",
        }
        result = dict(body_document)
        def metadata_present(value, key):
            candidate = value.get(key)
            if key == "author":
                return isinstance(candidate, dict) and any(
                    item is not None for item in candidate.values())
            return candidate is not None

        metadata_source = max(
            documents,
            key=lambda value: sum(
                metadata_present(value, key) for key in content_keys),
        )
        for key in content_keys:
            if not metadata_present(result, key) and metadata_present(
                    metadata_source, key):
                result[key] = metadata_source[key]
        supplemental = []
        for value in documents:
            if value is not body_document:
                supplemental = merge_media(supplemental, value.get("media") or [])
        result["media"] = merge_media(
            result.get("media") or [], supplemental)
        result["content_sha256"] = hashlib.sha256(json.dumps(
            {key: result.get(key) for key in ("id", "title", "body", "media")},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return result, metadata_source

    def _reader_client(self, protocol):
        client = self.clients.get(protocol)
        if client is None or client.config.protocol_mode != protocol:
            raise XhhConfigError(
                f"{protocol} reader requires an explicitly selected {protocol} session",
                code=f"{protocol}_session_missing",
            )
        return client

    def _platform_post_document(self, result, evidence, raw):
        try:
            link = result.get("link")
            if not isinstance(link, dict):
                raise XhhConfigError("post missing from platform result")
            return document(link, evidence, self.catalog)
        except XhhConfigError as error:
            error.code = "platform_response_invalid"
            error.raw_response = raw
            raise

    def _read_native_post(self, args, budget):
        query = {"link_id": args.link_id, "page": str(args.page),
                 "limit": str(args.limit), "sort_filter": args.sort}
        raw, result, evidence = self.native(
            query, budget, client=self._reader_client("app"))
        return self._platform_post_document(
            result, evidence, raw), raw

    def _read_website_post(self, args, budget):
        query = {"link_id": args.link_id, "page": str(args.page),
                 "limit": str(args.limit), "sort_filter": args.sort}
        raw, result, evidence = self.website(
            query, budget, client=self._reader_client("web"))
        return self._platform_post_document(
            result, evidence, raw), raw

    def _read_html_post(self, args, budget):
        return self.public_html(args.link_id, budget)

    def _reader_specs(self):
        handlers = {
            "native": self._read_native_post,
            "website": self._read_website_post,
            "html": self._read_html_post,
        }
        return {
            reader: replace(spec, handler=handlers[reader])
            for reader, spec in READERS.items()
        }

    def _reader_plan(self, args):
        specs = self._reader_specs()
        if args.reader != "auto":
            spec = specs[args.reader]
            if spec.protocol == "public":
                if args.protocol:
                    raise XhhConfigError(
                        "the html reader is public and cannot use --protocol")
            else:
                if args.protocol and args.protocol != spec.protocol:
                    raise XhhConfigError(
                        f"{spec.reader} reader requires the {spec.protocol} protocol")
                self._reader_client(spec.protocol)
            return [spec]

        protocols = (args.protocol,) if args.protocol else ("app", "web")
        plan = []
        for protocol in protocols:
            client = self.clients.get(protocol)
            if (
                    client is not None
                    and client.config.protocol_mode == protocol
                    and not (
                        getattr(client.config, "extra", None) or {}
                    ).get("public_reader")):
                plan.append(
                    specs["native"] if protocol == "app" else specs["website"])
        if not plan and args.protocol:
            selected = args.protocol or "app/web"
            raise XhhConfigError(
                f"auto reader has no available {selected} session",
                code=f"{args.protocol}_session_missing"
                if args.protocol else "session_missing",
            )
        plan.append(specs["html"])
        return plan

    @staticmethod
    def _reader_provenance(attempt):
        return {
            key: attempt.get(key)
            for key in (
                "reader", "protocol", "status", "body_status",
                "completeness", "body_characters", "route",
                "session_revision", "reader_offset", "identity_verified",
            )
        }

    @staticmethod
    def _body_candidate(attempt, value):
        body = value["body"]
        blocks = body.get("blocks") or []
        content = {
            "text": body.get("text"),
            "blocks": blocks,
        }
        return {
            "reader": attempt["reader"],
            "protocol": attempt["protocol"],
            "route": attempt["route"],
            "session_revision": attempt["session_revision"],
            "body_status": body.get("status"),
            "completeness": body.get("completeness"),
            "text_characters": len(body.get("text") or ""),
            "block_count": len(blocks),
            "block_types": sorted({
                block.get("type")
                for block in blocks
                if isinstance(block, dict) and isinstance(block.get("type"), str)
            }),
            "body_gaps": list(body.get("gaps") or []),
            "body_sha256": hashlib.sha256(json.dumps(
                content, sort_keys=True, ensure_ascii=False
            ).encode()).hexdigest(),
        }

    def _profile_relationship(self, user_id, user):
        follow_status = user.get("follow_status")
        current_user_follows_target = (
            follow_status
            if (
                self.client.config.protocol_mode == "app"
                and user_id != str(self.client.config.heybox_id)
                and type(follow_status) is int
                and follow_status in (0, 1)
            )
            else None
        )
        return {
            "current_user_follows_target": (
                bool(current_user_follows_target)
                if current_user_follows_target is not None
                else None
            ),
            "target_follows_current": None,
            "mutual": None,
            "evidence": "profile.user.follow_status",
        }

    @staticmethod
    def _document_has_body(value):
        body = value.get("body") if isinstance(value, dict) else None
        return (
            isinstance(body, dict)
            and body.get("status") == "available"
            and bool(body.get("text") or body.get("blocks"))
        )

    @staticmethod
    def _prior_post_body(prior_data, post_id):
        if (
                not isinstance(prior_data, dict)
                or identifier(prior_data.get("id")) != post_id
                or not Browser._document_has_body(prior_data)):
            return None
        return prior_data

    @staticmethod
    def _saved_body_attempt(saved):
        provenance = saved.get("provenance")
        attempt = (
            provenance.get("body")
            if isinstance(provenance, dict)
            else None
        )
        if isinstance(attempt, dict):
            return dict(attempt)
        body = saved.get("body") or {}
        evidence = saved.get("source") or {}
        return {
            "reader": evidence.get("reader"),
            "protocol": evidence.get("protocol"),
            "status": "ok",
            "body_status": body.get("status"),
            "completeness": body.get("completeness"),
            "body_characters": len(body.get("text") or ""),
            "route": evidence.get("route"),
            "session_revision": evidence.get("session_revision"),
            "reader_offset": None,
            "identity_verified": evidence.get("identity_verified"),
        }

    @staticmethod
    def _merge_body_candidates(previous, current):
        result = []
        seen = set()
        for candidate in [*(previous or []), *current]:
            if not isinstance(candidate, dict):
                continue
            key = json.dumps(candidate, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                result.append(candidate)
        return result

    def post(self, args, budget, *, plan=None, prior_data=None):
        attempts, documents, errors, raw_responses = [], [], [], []
        binding = query_key(args)
        plan = plan or self._reader_plan(args)
        reader_offset = 0
        reader_plan = [spec.reader for spec in plan]
        if args.resume:
            token = decode_resume(
                args.resume, "post", self.client.config.heybox_id)
            required = {
                "binding", "protocol", "position", "reader_offset",
                "reader_plan",
            }
            if (set(token) != required
                    or token.get("binding") != binding
                    or token.get("protocol") != self.client.config.protocol_mode
                    or type(token.get("position")) is not int
                    or token["position"] < 1
                    or type(token.get("reader_offset")) is not int
                    or not 0 <= token["reader_offset"] < len(plan)
                    or token.get("reader_plan") != reader_plan):
                raise XhhConfigError(
                    "post continuation does not match this read")
            reader_offset = token["reader_offset"]
        stop = None
        for index, spec in enumerate(
                plan[reader_offset:], start=reader_offset):
            session_revision = (
                self._session_revision(self.clients[spec.protocol])
                if spec.protocol in self.clients else None
            )
            attempt = {
                "reader": spec.reader,
                "protocol": spec.protocol,
                "route": (
                    spec.route.format(post_id=args.link_id)
                    if "{post_id}" in spec.route else spec.route
                ),
                "session_revision": session_revision,
                "reader_offset": index,
                "identity_verified": False,
                "status": "pending",
                "body_status": None,
                "completeness": None,
                "body_characters": None,
                "error": None,
            }
            attempts.append(attempt)
            try:
                value, raw = spec.handler(args, budget)
                documents.append(value)
                comment_result = None
                if isinstance(raw, dict):
                    try:
                        comment_result = result_object(raw)
                    except XhhError:
                        comment_result = raw
                if (isinstance(comment_result, dict)
                        and "comments" in comment_result):
                    try:
                        self._record_comment_floors(
                            comment_result,
                            value["source"],
                            args.link_id,
                            None,
                            position=args.page,
                            mode="page",
                        )
                    except XhhConfigError:
                        self.capture_results.append({
                            "persisted": False,
                            "reason": "interaction_page_invalid",
                        })
                if args.raw:
                    raw_responses.append({
                        "reader": spec.reader,
                        "protocol": spec.protocol,
                        "route": attempt["route"],
                        "response": raw,
                    })
                attempt.update({
                    "status": "ok",
                    "body_status": value["body"]["status"],
                    "completeness": value["body"]["completeness"],
                    "body_characters": len(value["body"]["text"]),
                    "identity_verified": value["source"]["identity_verified"],
                })
            except ReadLimit as error:
                attempt.update({
                    "status": "budget_stop",
                    "reason": error.reason,
                    "error": error.public(),
                })
                stop = StopState.budget_pause(error.reason)
                break
            except XhhError as error:
                reader_stop = StopState.from_error(error)
                attempt.update({
                    "status": "error",
                    "error": reader_stop.error,
                })
                errors.append(error)
                if reader_stop.kind == StopKind.BLOCKED:
                    stop = reader_stop
                    break
                continue
            if value["body"]["completeness"] == "complete":
                stop = StopState.complete()
                break
        if not documents:
            if stop is not None and stop.kind == StopKind.BLOCKED and errors:
                public = stop.error or {}
                terminal = errors[-1]
                if (terminal.code == public.get("code")
                        and terminal.retry_allowed is False):
                    raise terminal
                raise XhhError(
                    str(public.get("message") or "reader stopped"),
                    code=str(public.get("code") or "platform_error"),
                    fix=public.get("fix"),
                    retry_allowed=False,
                )
            terminal = next(
                (error for error in reversed(errors)
                 if not error.retry_allowed),
                None,
            )
            if terminal is not None:
                raise terminal
            if attempts and attempts[-1]["status"] == "budget_stop":
                raise ReadLimit(attempts[-1]["reason"])
            if errors:
                raise errors[-1]
            raise XhhConfigError(
                "no reader is available; store an App or Web session")
        selected_index, selected = max(
            enumerate(documents),
            key=lambda pair: (self._reader_score(pair[1]), -pair[0]),
        )
        successful = [
            attempt for attempt in attempts if attempt["status"] == "ok"
        ]
        retained_body = (
            self._prior_post_body(prior_data, args.link_id)
            if args.resume and not self._document_has_body(selected)
            else None
        )
        if retained_body is not None:
            data, metadata_source = self._merge_post_documents(
                [*documents, retained_body], retained_body)
            selected_attempt = self._saved_body_attempt(retained_body)
            body_gaps = data["body"].setdefault("gaps", [])
            if "reader_body_missing_after_resume" not in body_gaps:
                body_gaps.append("reader_body_missing_after_resume")
            if stop is None:
                stop = StopState(
                    StopKind.DATA_GAP,
                    reason="reader_body_missing_after_resume",
                )
        else:
            data, metadata_source = self._merge_post_documents(
                documents, selected)
            selected_attempt = successful[selected_index]
        if args.section != "metadata" and not self._document_has_body(selected):
            if stop is None and retained_body is None:
                public = XhhConfigError(
                    "reader returned no post body",
                    code="body_missing",
                    fix="use another reader or retry when the platform provides body content",
                ).public()
                stop = StopState.fatal("body_missing", public)
                selected_attempt["error"] = public
        if stop is None:
            stop = (
                StopState.complete()
                if args.reader != "auto"
                else StopState(
                    StopKind.DATA_GAP,
                    reason="reader_order_exhausted",
                )
            )
        saved_metadata = (
            metadata_source.get("provenance", {}).get("metadata")
            if metadata_source is retained_body
            else None
        )
        metadata_attempt = (
            dict(saved_metadata)
            if isinstance(saved_metadata, dict)
            else next(
                (
                    attempt for attempt in successful
                    if attempt["protocol"] == metadata_source["source"]["protocol"]
                    and attempt["route"] == metadata_source["source"]["route"]
                ),
                selected_attempt,
            )
        )
        data["reader_attempts"] = attempts
        data["selected_reader"] = data["source"]["reader"]
        current_candidates = [
            self._body_candidate(attempt, value)
            for attempt, value in zip(successful, documents)
        ]
        data["body_candidates"] = self._merge_body_candidates(
            retained_body.get("body_candidates", [])
            if retained_body is not None
            else [],
            current_candidates,
        )
        metadata_gaps = sorted({
            gap
            for value in (
                [*documents, retained_body]
                if retained_body is not None
                else documents
            )
            for gap in value.get("metadata_gaps", [])
        })
        if metadata_gaps:
            data["metadata_gaps"] = metadata_gaps
        data["provenance"] = {
            "metadata": self._reader_provenance(metadata_attempt),
            "body": self._reader_provenance(selected_attempt),
        }
        if args.raw:
            data["raw_responses"] = raw_responses
        scope = coverage({"has_more": False}, count=0, position=args.page, mode="page")
        scope.update(body=data["body"]["completeness"],
                     gaps=sorted(set(data["body"]["gaps"] + metadata_gaps)),
                     reader=data["source"]["reader"], readers_attempted=len(attempts),
                     reader_offset=reader_offset, sort=args.sort,
                     budget=budget.view())
        finalize_coverage(
            scope,
            stop,
            command="post",
            uid=self.client.config.heybox_id,
            protocol=self.client.config.protocol_mode,
            binding=binding,
            resume_parameters=(
                {
                    "position": args.page,
                    "reader_offset": attempts[-1]["reader_offset"],
                    "reader_plan": reader_plan,
                }
                if stop.kind == StopKind.BUDGET_PAUSE else None
            ),
        )
        self.capture_coverage(scope)
        if args.body_format != "both":
            data["body"].pop("text" if args.body_format == "blocks" else "blocks")
        if args.section == "metadata":
            data.pop("body")
        elif args.section == "body":
            keys = [
                "id", "url", "body", "source", "content_sha256",
                "reader_attempts", "body_candidates", "selected_reader",
                "provenance",
            ]
            if args.raw:
                keys.append("raw_responses")
            data = {key: data[key] for key in keys}
        return data, scope

    def page(self, args, budget, *, lossless=False):
        if getattr(args, "offset", 0) < 0:
            raise XhhConfigError("offset must be nonnegative")
        if args.command == "community":
            if args.action != "list" and not args.target:
                raise XhhConfigError("community action requires a target")
            if args.action in ("info", "children") and not identifier(args.target):
                raise XhhConfigError("community ID must be numeric")
            specs = {"list": ("/bbs/app/topic/categories", {}), "search": ("/bbs/app/topic/search", {"q": args.target}),
                     "info": ("/bbs/app/topic/list_infos", {"topic_ids": args.target}),
                     "children": ("/bbs/app/topic/sub/categories/v2", {"topic_id": args.target})}
            route, query = specs[args.action]
            raw, result, evidence = self.fetch(route, query, budget)
            data = {"items": communities(result), "source": evidence, "categories": result.get("categories")}
            scope = {"completion": "unknown", "full_history": False}
        elif args.command == "topics":
            raw, result, evidence = self.fetch("/bbs/app/api/post_editor/topic_selection/search",
                         {"q": args.query, "only_hashtag": "1"}, budget)
            data = {"items": rows(result, ("search_result", "hashtags", "topics")), "source": evidence}
            scope = {"completion": "unknown", "gaps": [], "full_history": False}
        elif args.command == "user":
            if args.view in ("profile", "posts"):
                raw, result, evidence = self.fetch("/bbs/app/profile/user/link/list", {"userid": args.user_id,
                            "offset": str(args.offset), "limit": "1" if args.view == "profile" else str(args.limit)}, budget)
                returned_user = result.get("user")
                if not isinstance(returned_user, dict):
                    raise XhhConfigError("public author identity not returned")
                if author({"user": returned_user})["uid"] != args.user_id:
                    raise XhhConfigError("returned author identity does not match requested user")
                if args.view == "posts":
                    records = rows(result, ("links",))
                    data = {"user_id": args.user_id, "items": [summary(row, self.catalog) for row in records], "source": evidence}
                    scope = coverage(result, count=len(records), position=args.offset)
                else:
                    user = returned_user
                    data = {**author({"user": user}), "bio": user.get("signature") or user.get("description"), "source": evidence,
                            "statistics": {"followers": integer(user.get("fan_num")), "following": integer(user.get("follow_num")),
                                           "posts": integer(user.get("post_link_num")), "articles": integer(user.get("post_article_num"))},
                            "relationship": self._profile_relationship(
                                args.user_id, user)}
                    scope = {"completion": "unknown", "gaps": [], "full_history": False}
            elif args.view == "relations":
                raw, result, evidence = self.fetch(
                    "/bbs/app/profile/relations",
                    {"userid": args.user_id}, budget)
                records = rows(result, ("users",))
                if any(not isinstance(row, dict) for row in records):
                    raise XhhConfigError("user records must be objects")
                count = result.get("count")
                if type(count) is not int or count < 0:
                    raise XhhConfigError("relation count must be a nonnegative integer")
                data = {
                    "items": [author(row if isinstance(row.get("user"), dict) else {"user": row}) for row in records],
                    "count": count,
                    "user_id": args.user_id,
                    "source": evidence,
                }
                scope = {"completion": "complete", "gaps": [], "next": None,
                         "returned": len(records), "full_history": False}
            else:
                route = "/bbs/app/profile/" + ("follower/list" if args.view == "followers" else "following/list")
                raw, result, evidence = self.fetch(route, {"userid": args.user_id, "offset": str(args.offset), "limit": str(args.limit)}, budget)
                records = (rows(result, ("follow_list",)) if "follow_list" in result
                           else rows(result, ("followers", "following", "users", "list")))
                if any(not isinstance(row, dict) for row in records):
                    raise XhhConfigError("user records must be objects")
                data = {"items": [author(row if isinstance(row.get("user"), dict) else {"user": row}) for row in records],
                        "user_id": args.user_id, "source": evidence}
                if "follow_cnt" in result:
                    counts = result["follow_cnt"]
                    if not isinstance(counts, dict):
                        raise XhhConfigError("follow counts must be an object")
                    follow_counts = {}
                    for key in ("fan_num", "follow_num", "inter_follow_num"):
                        value = counts.get(key)
                        if value is not None and (type(value) not in (int, str) or integer(value) is None):
                            raise XhhConfigError("follow count must be a nonnegative integer")
                        follow_counts[key] = integer(value)
                    data["follow_cnt"] = follow_counts
                    total = follow_counts["fan_num" if args.view == "followers" else "follow_num"]
                    if total is not None:
                        data["count"] = total
                        if "has_more_floors" not in result and "has_more" not in result:
                            result = {**result, "has_more": args.offset + len(records) < total}
                scope = coverage(result, count=len(records), position=args.offset)
        elif args.command == "favourites":
            if args.action == "folders":
                raw, result, evidence = self.fetch("/bbs/app/profile/fav/folders", {}, budget)
                records = rows(result, ("folders", "folder_list", "list"))
                data = {"items": [{"id": str(row.get("folder_id", row.get("id"))), "name": row.get("name") or row.get("title"),
                                    "count": integer(row.get("link_num", row.get("count")))} for row in records], "source": evidence}
                scope = {"completion": "unknown", "gaps": [], "full_history": False}
            else:
                if args.folder_id is None or not str(args.folder_id).isdigit():
                    raise XhhConfigError("favourites posts requires a numeric folder ID")
                raw, result, evidence = self.fetch("/bbs/app/profile/fav/folder/v2/links",
                            {"folder_id": args.folder_id, "offset": str(args.offset), "limit": str(args.limit)}, budget)
                records = rows(result, ("links", "list"))
                data = {"items": [summary(row, self.catalog) for row in records], "folder_id": args.folder_id, "source": evidence}
                scope = coverage(result, count=len(records), position=args.offset)
        elif args.command == "hashtags":
            if args.action in ("search", "links") and not args.target:
                raise XhhConfigError("hashtag action requires a target")
            if args.offset < 0:
                raise XhhConfigError("offset must be nonnegative")
            if args.action == "ranking":
                raw, result, evidence = self.fetch(
                    "/bbs/app/hashtag/ranking", {}, budget)
                records = first_rows(
                    result, ("ranking", "hot_hashtags", "hashtags", "items"))
                data = {
                    "items": [hashtag(row) for row in records],
                    "source": evidence,
                }
                scope = {"completion": "unknown", "gaps": [],
                         "full_history": False}
            elif args.action == "search":
                raw, result, evidence = self.fetch(
                    "/bbs/app/hashtag/search", {"q": args.target}, budget)
                records = first_rows(
                    result, ("hashtags", "items", "search_result", "list"))
                data = {
                    "items": [hashtag(row) for row in records],
                    "query": args.target,
                    "source": evidence,
                }
                scope = {"completion": "unknown", "gaps": [],
                         "full_history": False}
            else:
                if not args.name:
                    raise XhhConfigError(
                        "hashtags links requires --name from hashtag discovery")
                hashtag_id = numeric_argument(args.target)
                raw, result, evidence = self.fetch(
                    "/bbs/app/hashtag/link/list",
                    {"hashtag_id": hashtag_id, "hashtag_name": args.name,
                     "offset": str(args.offset), "limit": str(args.limit),
                     "h_src": ""},
                    budget,
                )
                items, count, invalid, gaps, occurrences = (
                    normalize_response(
                        result,
                        RESPONSES["hashtag-links"],
                        include_occurrences=True,
                    )
                )
                data = {
                    "items": items,
                    "hashtag": {"id": hashtag_id, "name": args.name},
                    "source": evidence,
                }
                if lossless:
                    data["occurrences"] = occurrences
                scope = coverage(result, count=count,
                                 position=args.offset)
                scope["gaps"].extend(gaps)
                scope.update(invalid=invalid)
                scope["contract_evidence"] = (
                    "implemented_contract_not_functionally_verified")
        elif args.command == "games" and args.action == "info":
            if not identifier(args.target):
                raise XhhConfigError("games info requires a numeric Steam app ID")
            if self.client.config.protocol_mode != "app":
                raise XhhConfigError("games info requires App protocol; select --protocol app")
            raw = None
            try:
                raw, result, evidence = self.fetch(
                    "/game/get_game_detail", {"appid": args.target}, budget)
                data, scope = game_detail(result, args.target, evidence)
            except (XhhError, TypeError, ValueError) as error:
                if args.raw:
                    raw = getattr(error, "raw_response", raw)
                    if raw is not None:
                        error.partial_data = {"raw_pages": [raw]}
                raise
        elif args.command == "games":
            if args.offset < 0:
                raise XhhConfigError("offset must be nonnegative")
            if args.action == "search":
                if not args.target:
                    raise XhhConfigError("games search requires a query")
                route, query = "/game/search", {
                    "q": args.target, "offset": str(args.offset),
                    "limit": str(args.limit)}
            elif args.action == "similar":
                if not args.target:
                    raise XhhConfigError("games similar requires a game ID")
                route, query = "/game/get_similar_games", {
                    "game_id": numeric_argument(args.target),
                    "offset": str(args.offset), "limit": str(args.limit)}
            else:
                route, query = "/game/all_recommend/v2", {
                    "offset": str(args.offset), "limit": str(args.limit)}
            raw, result, evidence = self.fetch(route, query, budget)
            items, count, invalid, gaps = normalize_response(result, RESPONSES["game"])
            data = {
                "items": items,
                "source": evidence,
                "action": args.action,
            }
            scope = coverage(result, count=count,
                             position=args.offset)
            scope["gaps"].extend(gaps)
            scope.update(invalid=invalid, excluded=invalid)
        elif args.command in ("stories", "recommended-users", "banners"):
            spec = RESPONSES[args.command]
            query = {} if args.command == "banners" else {
                "offset": str(args.offset), "limit": str(args.limit)}
            raw, result, evidence = self.fetch(spec.route, query, budget)
            items, count, invalid, gaps = normalize_response(result, spec)
            data = {"items": items, "source": evidence}
            if args.command == "banners":
                scope = {"completion": "unknown", "gaps": [],
                         "full_history": False}
            else:
                if (args.command == "recommended-users" and not count
                        and integer(result.get("recommend_nums")) == 0
                        and "has_more" not in result):
                    result = {**result, "has_more": False}
                scope = coverage(result, count=count, position=args.offset)
            scope["gaps"].extend(gaps)
            scope.update(invalid=invalid, excluded=invalid)
        elif args.command == "comments":
            query = {"link_id": args.link_id, "page": str(args.page), "limit": str(args.limit), "sort_filter": args.sort}
            raw, result, evidence = self.fetch(
                "/bbs/app/link/tree", query, budget)
            nodes, scope = self._record_comment_floors(
                result,
                evidence,
                args.link_id,
                None,
                position=args.page,
                mode="page",
                scope_updates={"sort": args.sort},
            )
            data = {"post_id": args.link_id, "items": nodes,
                    "source": evidence}
            data["available_sorts"] = result.get("sort_filter", [])
        elif args.command == "replies":
            query = {"root_comment_id": args.root_id, "hide_cy": "0", "h_src": ""}
            if args.cursor is not None:
                query["lastval"] = args.cursor
            raw, result, evidence = self.fetch("/bbs/app/comment/sub/comments", query, budget)
            nodes = rows(result, ("comments",))
            data = {"items": [comment(node, args.post_id, args.root_id, self.catalog) for node in nodes], "source": evidence,
                    "root_id": args.root_id, "post_id": args.post_id,
                    "post_id_source": "caller" if args.post_id else "unknown"}
            scope = coverage(result, count=len(nodes), position=args.cursor, mode="cursor")
            evidence["post_id_source"] = "caller" if args.post_id else "unknown"
            if args.post_id:
                self.record(data["items"], evidence, args.post_id, args.root_id, scope)
        else:
            if args.command == "search" and (args.hot_words or args.suggest):
                if args.hot_words and args.suggest:
                    raise XhhConfigError("choose one of --hot-words or --suggest")
                if args.hot_words:
                    route, query = "/bbs/app/api/search/hot_words", {}
                    mode = "hot_words"
                else:
                    route, query = "/bbs/app/api/search/suggestion/v2", {
                        "q": args.suggest}
                    mode = "suggestions"
                raw, result, evidence = self.fetch(route, query, budget)
                records = first_rows(
                    result, ("hot_words", "words", "suggestions", "items", "list"))
                data = {
                    "items": [row if not isinstance(row, str) else
                              {"text": row, "value": row} for row in records],
                    "source": evidence,
                    "discovery": mode,
                }
                scope = {"completion": "unknown", "gaps": [],
                         "full_history": False}
            else:
                if args.command == "search" and not args.query:
                    raise XhhConfigError(
                        "search requires QUERY, --hot-words, or --suggest QUERY")
                specs = {
                    "feed": ("/bbs/app/topic/feeds",
                             {"topic_id": getattr(args, "topic_id", ""),
                              "h_src": ""}),
                    "hot": ("/bbs/app/hot_news/main_list", {}),
                    "search": ("/bbs/app/api/general/search/v1", {
                        "q": getattr(args, "query", ""),
                        "search_type": getattr(args, "type", "link"),
                        "request_source": "edit_link"}),
                }
                route, query = specs[args.command]
                if args.command == "feed":
                    query.update(platform_parameters(args))
                if args.command == "search":
                    query.update(platform_parameters(args))
                    if args.community:
                        query["topic_id"] = numeric_argument(args.community)
                raw, result, evidence = self.fetch(
                    route, {**query, "offset": str(args.offset),
                            "limit": str(args.limit)}, budget)
                if args.command == "search":
                    items, count, invalid, gaps, occurrences = (
                        normalize_response(
                            result,
                            RESPONSES[args.type],
                            include_occurrences=True,
                            catalog=self.catalog,
                        )
                    )
                else:
                    items, count, invalid, gaps, occurrences = (
                        normalize_response(
                            result,
                            RESPONSES["feed"],
                            include_occurrences=True,
                            catalog=self.catalog,
                        )
                    )
                scope = coverage(result, count=count,
                                 position=args.offset)
                scope["gaps"].extend(gaps)
                if args.command in ("search", "feed"):
                    scope["invalid"] = invalid
                if args.command in ("feed", "search"):
                    if getattr(args, "type", "link") == "link" or args.command == "feed":
                        selected = []
                        for occurrence in occurrences:
                            reason = occurrence["reason"]
                            if (reason is None
                                    and occurrence["content"] is not None):
                                reason = post_filter_reason(
                                    args, occurrence["content"])
                                occurrence["reason"] = reason
                            if (reason is None
                                    and occurrence["content"] is not None):
                                selected.append(occurrence["content"])
                        items = selected
                    scope.update(
                        selected=len(items),
                        excluded=count - len(items),
                        sorting_scope="returned_items",
                        sort=args.sort,
                        platform_order=getattr(args, "order", None) or "default",
                        platform_sort_filter=query.get(
                            "sort_filter", "default"))
                data = {
                    "items": items,
                    "source": evidence,
                    "navigation": {
                        "sorts": result.get(
                            "sort_filter", result.get("sort_filter_list")),
                        "filters": result.get(
                            "filters", result.get("filter_list")),
                        "time_ranges": result.get("time_range_list"),
                        "forwardable_parameters": [
                            "sort_filter", "time_range"],
                    },
                }
                if lossless:
                    data["occurrences"] = occurrences
        if args.raw:
            data["raw"] = raw
        return data, scope

    def thread(self, args, budget, prior_data=None):
        nodes, root, page, cursor = {}, None, args.page, None
        target = args.around or args.root_id
        reply_location = None
        raw_pages = []
        last_scope = {"completion": "unknown", "gaps": []}
        phase = "comments"
        binding = query_key(args)
        root_page = args.page
        observed = set()
        new_items = set()
        stop = None
        item_window = None
        skipped = set()
        resume_parameters = None
        if args.resume:
            token = decode_resume(args.resume, "thread", self.client.config.heybox_id)
            if (token.get("binding") != binding or token.get("protocol") != self.client.config.protocol_mode
                    or token.get("phase") not in ("comments", "replies") or type(token.get("page")) is not int or token["page"] < 1
                    or token.get("cursor") is not None and (not isinstance(token["cursor"], str) or len(token["cursor"]) > 4096)):
                raise XhhConfigError("thread continuation does not match resource or filters")
            phase, page, cursor = token["phase"], token["page"], token.get("cursor")
            root_page = page
            if "item_window" in token:
                item_window = token["item_window"]
                item_phase = (
                    item_window.get("phase")
                    if isinstance(item_window, dict) else None
                )
                position = page if item_phase == "comments" else cursor
                if (not isinstance(item_window, dict)
                        or set(item_window) != {
                            "phase", "position", "consumed"}
                        or not (
                            item_phase == phase
                            or phase == "replies"
                            and item_phase == "comments"
                        )
                        or item_window["position"] != position
                        or not isinstance(item_window["consumed"], list)
                        or len(item_window["consumed"]) > 10000
                        or not all(
                            isinstance(key, str) and 0 < len(key) <= 256
                            for key in item_window["consumed"]
                        )):
                    raise XhhConfigError(
                        "thread item continuation is invalid or belongs to another position")
                skipped = set(item_window["consumed"])
            if prior_data is not None:
                if prior_data.get("post_id") != args.link_id or prior_data.get("root_id") != args.root_id:
                    raise XhhConfigError("stored thread belongs to a different target")
                nodes = {node["id"]: node for node in prior_data.get("items", [])}
                root = nodes.get(args.root_id) if phase == "replies" else None
                if prior_data.get("target_id") == target and target in nodes:
                    reply_location = prior_data.get("reply_location")
        for _ in range(args.pages):
            try:
                budget.reserve_page()
            except ReadLimit as error:
                stop = StopState.budget_pause(error.reason)
                break
            try:
                if root is None:
                    root_page = page
                    raw, result, evidence = self.fetch("/bbs/app/link/tree", {"link_id": args.link_id,
                                "page": str(page), "limit": str(args.limit), "sort_filter": args.sort}, budget)
                    if args.raw:
                        raw_pages.append(raw)
                    parsed, page_scope = self._record_comment_floors(
                        result,
                        evidence,
                        args.link_id,
                        args.root_id,
                        position=page,
                        mode="page",
                    )
                    page_keys, remainder = [], False
                    for node in parsed:
                        if node["root_id"] == args.root_id:
                            key = "comment:" + node["id"]
                            duplicate = node["id"] in nodes
                            if (not duplicate and node["id"] != args.root_id
                                    and key in skipped):
                                continue
                            if (not duplicate and node["id"] != args.root_id
                                    and len(new_items) >= budget.item_limit):
                                remainder = True
                                continue
                            node["source"] = evidence
                            nodes[node["id"]] = (enrich_observation(nodes[node["id"]], node)
                                                 if node["id"] in nodes else node)
                            observed.add(node["id"])
                            if not duplicate:
                                page_keys.append(key)
                                new_items.add(key)
                            if node["id"] == target:
                                reply_location = {"page": page, "limit": args.limit, "sort": args.sort, "cursor": None}
                    root = nodes.get(args.root_id)
                    last_scope = page_scope
                    page += 1
                    if remainder:
                        if budget.item_limit <= 1:
                            public = XhhConfigError(
                                "thread item limit cannot retain the root and a safe remainder",
                                code="unsafe_item_continuation",
                                fix="increase --limit to at least 2",
                            ).public()
                            stop = StopState(
                                StopKind.FATAL,
                                reason="unsafe_item_continuation",
                                error=public,
                            )
                        else:
                            stop = StopState.budget_pause("item_budget")
                            resume_parameters = {
                                "phase": "comments",
                                "page": root_page,
                                "cursor": None,
                                "item_window": {
                                    "phase": "comments",
                                    "position": root_page,
                                    "consumed": sorted(skipped | set(page_keys)),
                                },
                            }
                        break
                    if root is None:
                        if last_scope["completion"] == "complete" or last_scope["next"] is None:
                            stop = (
                                StopState.complete()
                                if last_scope["completion"] == "complete"
                                else StopState.fatal("continuation_missing")
                            )
                            break
                        continue
                    known_children = len(nodes) - 1
                    root_page = page - 1
                    if root["has_more"] is not True and (root["has_more"] is False and (root["children_count"] is None or known_children >= root["children_count"])
                            or root["children_count"] is not None and known_children >= root["children_count"]):
                        last_scope["completion"] = "complete"
                        stop = StopState.complete()
                        break
                    if len(new_items) >= budget.item_limit:
                        stop = StopState.budget_pause("item_budget")
                        resume_parameters = {
                            "phase": "replies",
                            "page": root_page,
                            "cursor": None,
                            "item_window": {
                                "phase": "comments",
                                "position": root_page,
                                "consumed": sorted(
                                    skipped | set(page_keys)),
                            },
                        }
                        break
                    phase = "replies"
                    last_scope["completion"] = "more" if root["has_more"] is True else "unknown"
                else:
                    query = {"root_comment_id": args.root_id, "hide_cy": "0", "h_src": ""}
                    if cursor is not None:
                        query["lastval"] = cursor
                    raw, result, evidence = self.fetch("/bbs/app/comment/sub/comments", query, budget)
                    if args.raw:
                        raw_pages.append(raw)
                    records = rows(result, ("comments",))
                    parsed = [
                        comment(item, args.link_id, args.root_id, self.catalog)
                        for item in records
                    ]
                    last_scope = coverage(
                        result,
                        count=len(records),
                        position=cursor,
                        mode="cursor",
                    )
                    self.record(
                        parsed,
                        evidence,
                        args.link_id,
                        args.root_id,
                        last_scope,
                    )
                    page_keys, remainder = [], False
                    for node in parsed:
                        key = "comment:" + node["id"]
                        duplicate = node["id"] in nodes
                        if not duplicate and key in skipped:
                            continue
                        if (not duplicate
                                and len(new_items) >= budget.item_limit):
                            remainder = True
                            continue
                        node["source"] = evidence
                        nodes[node["id"]] = (enrich_observation(nodes[node["id"]], node)
                                             if node["id"] in nodes else node)
                        observed.add(node["id"])
                        if not duplicate:
                            page_keys.append(key)
                            new_items.add(key)
                        if node["id"] == target:
                            reply_location = {"page": root_page, "limit": args.limit, "sort": args.sort, "cursor": cursor}
                    if remainder:
                        if budget.item_limit <= 1:
                            public = XhhConfigError(
                                "thread item limit cannot retain the root and a safe remainder",
                                code="unsafe_item_continuation",
                                fix="increase --limit to at least 2",
                            ).public()
                            stop = StopState(
                                StopKind.FATAL,
                                reason="unsafe_item_continuation",
                                error=public,
                            )
                        else:
                            stop = StopState.budget_pause("item_budget")
                            resume_parameters = {
                                "phase": "replies",
                                "page": root_page,
                                "cursor": cursor,
                                "item_window": {
                                    "phase": "replies",
                                    "position": cursor,
                                    "consumed": sorted(skipped | set(page_keys)),
                                },
                            }
                        break
                    if last_scope["completion"] == "complete" or last_scope["next"] is None:
                        stop = (
                            StopState.complete()
                            if last_scope["completion"] == "complete"
                            else StopState.fatal("continuation_missing")
                        )
                        break
                    if last_scope["next"] == cursor:
                        stop = StopState.cursor_stalled()
                        break
                    if len(new_items) >= budget.item_limit:
                        stop = StopState.budget_pause("item_budget")
                        resume_parameters = {
                            "phase": "replies",
                            "page": root_page,
                            "cursor": last_scope["next"],
                        }
                        break
                    cursor = last_scope["next"]
            except ReadLimit as error:
                stop = StopState.budget_pause(error.reason)
                break
            except XhhError as error:
                if not nodes:
                    raise
                stop = StopState.from_error(error)
                if stop.kind == StopKind.RETRYABLE_FAILURE:
                    resume_parameters = {
                        "phase": phase,
                        "page": page if phase == "comments" else root_page,
                        "cursor": cursor,
                    }
                break
        if resume_parameters is None:
            resume_parameters = {
                "phase": phase,
                "page": page if phase == "comments" else root_page,
                "cursor": cursor,
            }
        if stop is None:
            if last_scope.get("completion") == "complete":
                stop = StopState.complete()
            else:
                stop = StopState.budget_pause("page_budget")
        target = args.around or args.root_id
        ancestors, missing, visited, unknown_parents = [], [], set(), []
        node = nodes.get(target)
        while node and node["id"] not in visited:
            visited.add(node["id"])
            ancestors.append(node["id"])
            parent = node["reply_to_id"]
            if parent is None:
                if node["id"] != args.root_id:
                    unknown_parents.append(node["id"])
                    last_scope["gaps"].append("reply_target_unknown")
                break
            if parent in visited:
                last_scope["gaps"].append("cyclic_reply_reference")
                break
            if parent not in nodes:
                missing.append(parent)
                break
            node = nodes[parent]
        if target not in nodes:
            last_scope["gaps"].append("target_not_observed")
        if missing:
            last_scope["gaps"].append("ancestors_missing")
        budget.consume_items(len(new_items))
        last_scope["budget"] = budget.view()
        finalize_coverage(
            last_scope,
            stop,
            command="thread",
            uid=self.client.config.heybox_id,
            protocol=self.client.config.protocol_mode,
            binding=binding,
            resume_parameters=resume_parameters,
        )
        last_scope["continuation"] = (
            {
                "command": resume_parameters["phase"],
                "post_id": args.link_id,
                "root_id": args.root_id,
                "page": (resume_parameters["page"]
                         if resume_parameters["phase"] == "comments" else None),
                "cursor": resume_parameters["cursor"],
            }
            if last_scope.get("resume") else None
        )
        data = {"post_id": args.link_id, "root_id": args.root_id, "target_id": target, "target_observed": target in nodes,
                "target_observed_this_read": target in observed,
                "reply_location": reply_location,
                "ancestor_ids": ancestors, "missing_ancestor_ids": missing, "unknown_parent_ids": unknown_parents,
                "ancestry_complete": bool(ancestors) and ancestors[-1] == args.root_id and not missing and not unknown_parents,
                "items": list(nodes.values())}
        if args.raw:
            data["raw_pages"] = raw_pages
        self.capture_coverage(last_scope)
        return data, last_scope
