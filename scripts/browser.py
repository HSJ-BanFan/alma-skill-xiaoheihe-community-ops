"""Community discovery and bounded platform reading."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from documents import author, comment, document, floors, identifier, integer, link_id, merge_media, result_object, source, summary, entity_id, enrich_observation
from network_io import read_http
from paging import Budget, ReadLimit, coverage, decode_resume, encode_resume
from xhh_sdk.exceptions import (
    XhhAuthError,
    XhhCaptchaError,
    XhhConfigError,
    XhhError,
    XhhRateLimitError,
)
from xhh_sdk.web_signer import sign_web_request

COMMANDS = {
    "community", "topics", "hot", "feed", "search", "post", "comments",
    "replies", "thread", "user", "favourites", "hashtags", "stories",
    "banners", "recommended-users", "games",
}


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
        return
    selected = data["items"][:limit]
    data["items"] = selected
    scope.update(matched_total=matched, selected=len(selected), limit=limit,
                 limit_reached=matched > limit, truncated=matched > limit)


class PublicPageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.article_depth = 0
        self.in_title = False
        self.title = []
        self.parts = []
        self.json_ld = []
        self._script = None

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        self.depth += 1
        if tag == "article" or (tag == "main" and not self.article_depth):
            self.article_depth = self.depth
        if tag == "title":
            self.in_title = True
        if tag == "script":
            kind = str(values.get("type") or "").casefold()
            if kind == "application/ld+json":
                self._script = []
        if self.article_depth and tag in {
                "p", "div", "li", "blockquote", "pre", "h1", "h2", "h3",
                "h4", "h5", "h6", "tr", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            self.json_ld.append("".join(self._script))
            self._script = None
        if tag == "title":
            self.in_title = False
        if self.article_depth and tag in {
                "p", "div", "li", "blockquote", "pre", "h1", "h2", "h3",
                "h4", "h5", "h6", "tr"}:
            self.parts.append("\n")
        if self.article_depth == self.depth:
            self.article_depth = 0
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        if self.in_title:
            self.title.append(data)
        if self.article_depth:
            self.parts.append(data)


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


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
            parser.add_argument("action", choices=("search", "recommended", "similar"))
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
                parser.add_argument("--reader", choices=("native", "website", "html", "auto"), default="native")
                parser.add_argument("--section", choices=("all", "body", "metadata"), default="all")
                parser.add_argument("--body-format", choices=("text", "blocks", "both"), default="both")
            if name == "thread":
                parser.add_argument("root_id", type=numeric_argument)
                parser.add_argument("--around", type=numeric_argument)
        elif name == "replies":
            parser.add_argument("root_id", type=numeric_argument)
            parser.add_argument("--post-id", type=post_argument)
            parser.add_argument("--cursor")
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
                     or raw.get("gameid") or raw.get("id"))
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
        "icon_url": raw.get("icon") or raw.get("icon_url"),
        "cover_url": raw.get("cover") or raw.get("cover_url"),
        "platforms": raw.get("platforms") or raw.get("platform"),
        "tags": raw.get("tags") or raw.get("game_tags"),
        "communities": communities or None,
        "navigation": {
            "feed": ({"command": "feed", "topic_id": communities[0]["id"]}
                     if communities else None),
            "similar_games": {"command": "games similar", "game_id": uid},
        },
    }


def relation(raw, user_id, current_user_id):
    if not isinstance(raw, dict):
        raise XhhConfigError("relation summary must be an object")
    first = lambda *keys: next((raw[key] for key in keys if key in raw), None)
    following = raw.get("is_follow") if "is_follow" in raw else raw.get("following")
    followed_by = raw.get("is_fans") if "is_fans" in raw else raw.get("followed_by")
    following = following if following in (True, False, 0, 1) else None
    followed_by = followed_by if followed_by in (True, False, 0, 1) else None
    following = bool(following) if following is not None else None
    followed_by = bool(followed_by) if followed_by is not None else None
    return {
        "user_id": user_id,
        "current_user_id": current_user_id,
        "following": following,
        "followed_by_target": followed_by,
        "mutual": (following and followed_by
                   if following is not None and followed_by is not None else None),
        "friend": first("is_friend", "friend"),
        "blocked": first("is_block", "blocked"),
        "following_since": first("follow_time", "following_since"),
        "categories": first("categories", "relation_types"),
        "statistics": {
            "followers": integer(first("fan_num", "fans_num")),
            "following": integer(first("follow_num", "following_num")),
        },
    }


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
    def __init__(self, client, *, clients=None):
        self.client = client
        self.clients = dict(clients or {client.config.protocol_mode: client})
        self.identity_verified = False

    def fetch(self, route, query, budget, *, client=None):
        selected = client or self.client
        raw = budget.call(selected, lambda: selected.transport.signed_request(route, query=query))
        if route == "/bbs/app/profile/user/link/list" and isinstance(raw.get("post_links"), list):
            result = {"links": raw["post_links"], "user": raw.get("user"), "has_more": raw.get("has_more")}
        else:
            result = result_object(raw)
        evidence = source(selected, route)
        evidence["identity_verified"] = self.identity_verified
        return raw, result, evidence

    def execute(self, args, *, verify_account=False, prior_data=None):
        budget = Budget(args.requests, args.seconds)
        if verify_account:
            from operations import verify_identity
            if args.requests < 2:
                raise XhhConfigError("saving, resuming, or comparing observations requires at least two requests including account verification")
            budget.call(self.client, lambda: verify_identity(self.client))
            self.identity_verified = True
        if args.command == "post" and args.reader == "auto":
            return self.post_auto(args, budget)
        if args.command == "thread":
            return self.thread(args, budget, prior_data)
        paged = (args.command in ("comments", "replies", "feed", "hot", "stories",
                                  "recommended-users")
                 or args.command == "search" and not (
                     args.hot_words or args.suggest)
                 or args.command == "user" and args.view != "profile"
                 or args.command == "hashtags" and args.action == "links"
                 or args.command == "games"
                 or args.command == "favourites" and args.action == "posts")
        if not paged and (args.pages != 1 or args.resume):
            raise XhhConfigError("this command returns one resource; pages/resume are not applicable")
        mode = "page" if args.command == "comments" else "cursor" if args.command == "replies" else "offset"
        position_key = {"page": "page", "cursor": "cursor", "offset": "offset"}[mode]
        binding = hashlib.sha256(json.dumps({key: value for key, value in vars(args).items()
                    if key not in {"resume", "pages", "requests", "seconds", "raw", "snapshot", "since", position_key,
                                   "account", "config", "env", "protocol", "data_dir"}},
                    sort_keys=True, default=str).encode()).hexdigest()
        if args.resume:
            token = decode_resume(args.resume, args.command, self.client.config.heybox_id)
            if set(token) != {"binding", "position", "protocol"} or token["binding"] != binding or token["protocol"] != self.client.config.protocol_mode:
                raise XhhConfigError("resume does not match the requested resource or filters")
            value = token["position"]
            if mode == "cursor":
                if not isinstance(value, str) or len(value) > 4096:
                    raise XhhConfigError("invalid continuation cursor")
            elif type(value) is not int or value < (1 if mode == "page" else 0):
                raise XhhConfigError("invalid continuation position")
            setattr(args, position_key, value)
        items, identities, positions, raw_pages = [], set(), set(), []
        observed_positions = []
        data, scope = None, {"completion": "unknown", "gaps": [], "next": getattr(args, position_key, None)}
        duplicates = 0
        identity_positions = {}
        returned_total, excluded_total = 0, 0
        for _ in range(args.pages):
            position = getattr(args, position_key, None)
            if position in positions and paged:
                scope["stop_reason"] = "cursor_stalled"
                scope["gaps"].append("repeated_continuation")
                break
            positions.add(position)
            try:
                page, current = self.page(args, budget)
            except ReadLimit as error:
                scope["stop_reason"] = error.reason
                break
            except XhhError as error:
                if data is None:
                    raise
                scope["stop_reason"] = "platform_error"
                scope["error"] = error.public()
                break
            data = page
            if paged:
                observed_positions.append(position)
            if args.raw and "raw" in page:
                raw_pages.append(page.pop("raw"))
            scope = current
            if paged and mode == "cursor" and current["next"] is not None and current["next"] == position:
                scope["stop_reason"] = "cursor_stalled"
                scope["gaps"].append("repeated_continuation")
                scope["next"] = None
            returned_total += current.get("returned", 0)
            excluded_total += current.get("excluded", 0)
            if not paged:
                break
            for item in page["items"]:
                identity = entity_id(item) if isinstance(item, dict) else None
                if identity and identity in identities:
                    duplicates += 1
                    index = identity_positions[identity]
                    items[index] = enrich_observation(items[index], item)
                    continue
                if identity:
                    identities.add(identity)
                    identity_positions[identity] = len(items)
                items.append(item)
            if current["completion"] == "complete" or current["next"] is None:
                break
            setattr(args, position_key, current["next"])
        if data is None:
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
            if args.command == "feed":
                scope.update(selected=len(items), excluded=excluded_total)
            matched_total = len(items)
            _fit_items(data, args, scope, matched=matched_total)
            if (scope.get("completion") != "complete"
                    and scope.get("next") is not None
                    and not scope.get("stop_reason")):
                scope["stop_reason"] = "page_budget"
        elif isinstance(data.get("items"), list) and hasattr(args, "limit"):
            _fit_items(data, args, scope, matched=len(data["items"]))
        if args.raw:
            data["raw_pages"] = raw_pages
        if paged and scope.get("completion") != "complete" and scope.get("next") is not None and scope.get("stop_reason") != "cursor_stalled":
            scope["resume"] = encode_resume(args.command, self.client.config.heybox_id,
                                {"binding": binding, "position": scope["next"], "protocol": self.client.config.protocol_mode})
        else:
            scope["resume"] = None
        scope["budget"] = budget.view()
        scope["observed_positions"] = observed_positions
        return data, scope

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
        evidence["identity_verified"] = self.identity_verified
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
        body = budget.call(
            self.client,
            lambda: read_http(request, max(0.01, budget.deadline - time.monotonic()),
                              profile="public", http_timeout=self.client.config.timeout),
        )
        try:
            page = body.decode("utf-8")
        except UnicodeDecodeError:
            page = body.decode("utf-8", errors="replace")
        parser = PublicPageParser()
        parser.feed(page)
        title = re.sub(r"\s+", " ", "".join(parser.title)).strip() or None
        article_body = None
        for script in parser.json_ld:
            try:
                decoded = json.loads(script)
            except ValueError:
                continue
            for node in _walk_json(decoded):
                candidate = node.get("articleBody")
                if isinstance(candidate, str) and candidate.strip():
                    article_body = candidate.strip()
                    break
            if article_body:
                break
        text = article_body or re.sub(r"[ \t]+", " ", "".join(parser.parts))
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        link = {"id": post_id, "title": title, "text": text}
        evidence = {"route": route, "protocol": "html", "observed_at": time.time(),
                    "configured_uid": str(self.client.config.heybox_id),
                    "identity_verified": False, "reader": "html",
                    "credentials_sent": False}
        data = document(link, evidence)
        if not text:
            data["body"]["gaps"] = sorted(set(
                data["body"]["gaps"] + ["html_article_not_found"]))
        return data

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
        metadata_source = max(
            documents,
            key=lambda value: sum(
                value.get(key) is not None for key in content_keys),
        )
        for key in content_keys:
            if result.get(key) is None and metadata_source.get(key) is not None:
                result[key] = metadata_source[key]
        supplemental = []
        for value in documents:
            if value is not body_document:
                supplemental = merge_media(supplemental, value.get("media") or [])
        result["media"] = merge_media(
            result.get("media") or [], supplemental)
        result["content_sha256"] = hashlib.sha256(json.dumps(
            {key: result[key] for key in ("id", "title", "body", "media")},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return result

    def post_auto(self, args, budget):
        query = {"link_id": args.link_id, "page": str(args.page),
                 "limit": str(args.limit), "sort_filter": args.sort}
        attempts, documents, errors = [], [], []
        order = [protocol for protocol in ("app", "web") if protocol in self.clients]
        for protocol in order:
            reader = "native" if protocol == "app" else "website"
            try:
                raw, result, evidence = (
                    self.native(query, budget, client=self.clients[protocol])
                    if protocol == "app"
                    else self.website(query, budget, client=self.clients[protocol])
                )
                if not isinstance(result.get("link"), dict):
                    raise XhhConfigError("post missing from platform result")
                value = document(result["link"], evidence)
                documents.append(value)
                attempts.append({
                    "reader": reader, "protocol": protocol, "status": "ok",
                    "body_status": value["body"]["status"],
                    "completeness": value["body"]["completeness"],
                    "body_characters": len(value["body"]["text"]),
                })
            except (XhhCaptchaError, XhhRateLimitError):
                raise
            except ReadLimit as error:
                attempts.append({
                    "reader": reader, "protocol": protocol, "status": "budget_stop",
                    "reason": error.reason,
                })
                break
            except XhhError as error:
                attempts.append({
                    "reader": reader, "protocol": protocol, "status": "error",
                    "error": error.public(),
                })
                errors.append(error)
        needs_html = not documents or self._reader_score(
            max(documents, key=self._reader_score))[0] < 3
        if needs_html:
            try:
                html = self.public_html(args.link_id, budget)
                documents.append(html)
                attempts.append({
                    "reader": "html", "protocol": "html", "status": "ok",
                    "body_status": html["body"]["status"],
                    "completeness": html["body"]["completeness"],
                    "body_characters": len(html["body"]["text"]),
                })
            except XhhCaptchaError:
                raise
            except ReadLimit as error:
                attempts.append({"reader": "html", "protocol": "html",
                                 "status": "budget_stop", "reason": error.reason})
            except XhhError as error:
                attempts.append({"reader": "html", "protocol": "html",
                                 "status": "error", "error": error.public()})
                errors.append(error)
        if not documents:
            raise errors[0] if errors else XhhConfigError(
                "no reader is available; store an App or Web session")
        selected = max(enumerate(documents), key=lambda pair: (
            self._reader_score(pair[1]), -pair[0]))[1]
        data = self._merge_post_documents(documents, selected)
        data["reader_attempts"] = attempts
        data["selected_reader"] = data["source"]["reader"]
        scope = coverage({"has_more": False}, count=0, position=args.page, mode="page")
        scope.update(body=data["body"]["completeness"], gaps=list(data["body"]["gaps"]),
                     reader=data["source"]["reader"], readers_attempted=len(attempts),
                     sort=args.sort, budget=budget.view())
        if args.body_format != "both":
            data["body"].pop("text" if args.body_format == "blocks" else "blocks")
        if args.section == "metadata":
            data.pop("body")
        elif args.section == "body":
            data = {key: data[key] for key in (
                "id", "url", "body", "source", "content_sha256",
                "reader_attempts", "selected_reader")}
        return data, scope

    def page(self, args, budget):
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
            scope = {"completion": "unknown", "full_history": False}
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
                    data = {"user_id": args.user_id, "items": [summary(row) for row in records], "source": evidence}
                    scope = coverage(result, count=len(records), position=args.offset)
                else:
                    user = returned_user
                    data = {**author({"user": user}), "bio": user.get("signature") or user.get("description"), "source": evidence,
                            "statistics": {"followers": integer(user.get("fan_num")), "following": integer(user.get("follow_num")),
                                           "posts": integer(user.get("post_link_num")), "articles": integer(user.get("post_article_num"))}}
                    scope = {"completion": "unknown", "gaps": [], "full_history": False}
            elif args.view == "relations":
                raw, result, evidence = self.fetch(
                    "/bbs/app/profile/relations",
                    {"userid": args.user_id}, budget)
                returned_id = identifier(
                    result.get("userid") or result.get("user_id"))
                if returned_id is not None and returned_id != args.user_id:
                    raise XhhConfigError(
                        "relation summary belongs to a different user")
                data = {
                    "relation": relation(
                        result, args.user_id, self.client.config.heybox_id),
                    "source": evidence,
                }
                scope = {"completion": "unknown", "gaps": [],
                         "full_history": False}
            else:
                route = "/bbs/app/profile/" + ("follower/list" if args.view == "followers" else "following/list")
                raw, result, evidence = self.fetch(route, {"userid": args.user_id, "offset": str(args.offset), "limit": str(args.limit)}, budget)
                records = rows(result, ("followers", "following", "users", "list"))
                data = {"items": [author(row if isinstance(row.get("user"), dict) else {"user": row}) for row in records],
                        "user_id": args.user_id, "source": evidence}
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
                data = {"items": [summary(row) for row in records], "folder_id": args.folder_id, "source": evidence}
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
                records = first_rows(result, ("links", "items", "list"))
                data = {
                    "items": [summary(row) for row in records],
                    "hashtag": {"id": hashtag_id, "name": args.name},
                    "source": evidence,
                }
                scope = coverage(result, count=len(records),
                                 position=args.offset)
                scope["contract_evidence"] = (
                    "implemented_contract_not_functionally_verified")
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
            records = first_rows(
                result, ("games", "items", "list", "recommend", "similar"))
            data = {
                "items": [game(row) for row in records],
                "source": evidence,
                "action": args.action,
            }
            scope = coverage(result, count=len(records),
                             position=args.offset)
        elif args.command in ("stories", "recommended-users", "banners"):
            if args.command == "banners":
                raw, result, evidence = self.fetch(
                    "/bbs/app/feeds/banner", {}, budget)
                records = first_rows(
                    result, ("banners", "items", "list"))
                data = {
                    "items": records,
                    "source": evidence,
                    "contract_evidence": "historical_default_call_ok",
                }
                scope = {"completion": "unknown", "gaps": [],
                         "full_history": False}
            elif args.command == "stories":
                raw, result, evidence = self.fetch(
                    "/bbs/app/story_mode/list",
                    {"offset": str(args.offset), "limit": str(args.limit)},
                    budget)
                records = first_rows(result, ("links", "items", "list"))
                data = {
                    "items": [summary(row) if isinstance(
                        row.get("linkid") or row.get("link_id"), (str, int)) else row
                              for row in records],
                    "source": evidence,
                }
                scope = coverage(result, count=len(records),
                                 position=args.offset)
            else:
                raw, result, evidence = self.fetch(
                    "/bbs/app/profile/recommend/following",
                    {"offset": str(args.offset), "limit": str(args.limit)},
                    budget)
                records = first_rows(result, ("users", "items", "recommend", "list"))
                data = {
                    "items": [author(row) for row in records],
                    "source": evidence,
                }
                scope = coverage(result, count=len(records),
                                 position=args.offset)
        elif args.command in ("post", "comments"):
            query = {"link_id": args.link_id, "page": str(args.page), "limit": str(args.limit), "sort_filter": args.sort}
            reader = getattr(args, "reader", "native")
            if reader == "auto":
                reader = "website" if self.client.config.protocol_mode == "web" else "native"
            raw, result, evidence = self.website(query, budget) if reader == "website" else self.fetch("/bbs/app/link/tree", query, budget)
            if args.command == "post" and not isinstance(result.get("link"), dict):
                raise XhhConfigError("post missing from platform result")
            nodes = floors(result.get("comments"), args.link_id) if args.command == "comments" else []
            data = document(result["link"], evidence) if args.command == "post" else {"post_id": args.link_id, "items": nodes, "source": evidence}
            scope = coverage(result, count=len(result.get("comments", [])), position=args.page, mode="page")
            if "comments" not in result:
                scope["gaps"].append("comments_not_returned")
            scope["sort"] = args.sort
            if args.command == "post":
                evidence["reader"] = reader
                scope["body"] = data["body"]["completeness"]
                scope["gaps"].extend(data["body"]["gaps"])
                if args.body_format != "both":
                    data["body"].pop("text" if args.body_format == "blocks" else "blocks")
                if args.section == "metadata":
                    data.pop("body")
                elif args.section == "body":
                    data = {key: data[key] for key in ("id", "url", "body", "source", "content_sha256")}
            data["available_sorts"] = result.get("sort_filter", [])
        elif args.command == "replies":
            query = {"root_comment_id": args.root_id, "hide_cy": "0", "h_src": ""}
            if args.cursor is not None:
                query["lastval"] = args.cursor
            raw, result, evidence = self.fetch("/bbs/app/comment/sub/comments", query, budget)
            nodes = rows(result, ("comments",))
            data = {"items": [comment(node, args.post_id, args.root_id) for node in nodes], "source": evidence,
                    "root_id": args.root_id, "post_id": args.post_id,
                    "post_id_source": "caller" if args.post_id else "unknown"}
            scope = coverage(result, count=len(nodes), position=args.cursor, mode="cursor")
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
                records = first_rows(
                    result, ("links", "search_result", "items", "list", "news"))
                if args.command != "search" or args.type == "link":
                    items = [summary(row) for row in records]
                elif args.type == "game":
                    items = [game(row.get("game", row)) for row in records]
                else:
                    items = [author(row.get("user", row)) for row in records]
                scope = coverage(result, count=len(records),
                                 position=args.offset)
                if args.command in ("feed", "search"):
                    if getattr(args, "type", "link") == "link" or args.command == "feed":
                        items = [
                            item for item in items
                            if (args.content_type == "all"
                                or item["content_type"] == args.content_type)
                            and (args.after is None
                                 or item["created_at"] is not None
                                 and item["created_at"] >= args.after)
                            and (item["statistics"]["comments"] or 0)
                            >= args.min_comments
                        ]
                    scope.update(
                        selected=len(items),
                        excluded=len(records) - len(items),
                        sorting_scope="returned_items",
                        sort=args.sort,
                        platform_order=query.get("sort_filter", "default"))
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
        resumable = True
        if args.resume:
            token = decode_resume(args.resume, "thread", self.client.config.heybox_id)
            if (token.get("binding") != binding or token.get("protocol") != self.client.config.protocol_mode
                    or token.get("phase") not in ("comments", "replies") or type(token.get("page")) is not int or token["page"] < 1
                    or token.get("cursor") is not None and (not isinstance(token["cursor"], str) or len(token["cursor"]) > 4096)):
                raise XhhConfigError("thread continuation does not match resource or filters")
            phase, page, cursor = token["phase"], token["page"], token.get("cursor")
            root_page = page
            if prior_data is not None:
                if prior_data.get("post_id") != args.link_id or prior_data.get("root_id") != args.root_id:
                    raise XhhConfigError("stored thread belongs to a different target")
                nodes = {node["id"]: node for node in prior_data.get("items", [])}
                root = nodes.get(args.root_id) if phase == "replies" else None
                if prior_data.get("target_id") == target and target in nodes:
                    reply_location = prior_data.get("reply_location")
        for _ in range(args.pages):
            try:
                if root is None:
                    raw, result, evidence = self.fetch("/bbs/app/link/tree", {"link_id": args.link_id,
                                "page": str(page), "limit": str(args.limit), "sort_filter": args.sort}, budget)
                    if args.raw:
                        raw_pages.append(raw)
                    parsed = floors(result.get("comments"), args.link_id)
                    for node in parsed:
                        if node["root_id"] == args.root_id:
                            node["source"] = evidence
                            nodes[node["id"]] = (enrich_observation(nodes[node["id"]], node)
                                                 if node["id"] in nodes else node)
                            observed.add(node["id"])
                            if node["id"] == target:
                                reply_location = {"page": page, "limit": args.limit, "sort": args.sort, "cursor": None}
                    root = nodes.get(args.root_id)
                    last_scope = coverage(result, count=len(result["comments"]), position=page, mode="page")
                    page += 1
                    if root is None:
                        if last_scope["completion"] == "complete" or last_scope["next"] is None:
                            resumable = False
                            break
                        continue
                    known_children = len(nodes) - 1
                    root_page = page - 1
                    if root["has_more"] is not True and (root["has_more"] is False and (root["children_count"] is None or known_children >= root["children_count"])
                            or root["children_count"] is not None and known_children >= root["children_count"]):
                        last_scope["completion"] = "complete"
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
                    for item in records:
                        node = comment(item, args.link_id, args.root_id)
                        node["source"] = evidence
                        nodes[node["id"]] = (enrich_observation(nodes[node["id"]], node)
                                             if node["id"] in nodes else node)
                        observed.add(node["id"])
                        if node["id"] == target:
                            reply_location = {"page": root_page, "limit": args.limit, "sort": args.sort, "cursor": cursor}
                    last_scope = coverage(result, count=len(records), position=cursor, mode="cursor")
                    if last_scope["completion"] == "complete" or last_scope["next"] is None:
                        resumable = False
                        break
                    if last_scope["next"] == cursor:
                        last_scope["stop_reason"] = "cursor_stalled"
                        resumable = False
                        break
                    cursor = last_scope["next"]
            except ReadLimit as error:
                last_scope["stop_reason"] = error.reason
                break
            except XhhError as error:
                if not nodes:
                    raise
                last_scope["stop_reason"] = "platform_error"
                last_scope["error"] = error.public()
                break
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
        last_scope["continuation"] = {"command": phase, "post_id": args.link_id, "root_id": args.root_id,
                                      "page": page if phase == "comments" else None, "cursor": cursor} if resumable and last_scope["completion"] != "complete" else None
        last_scope["budget"] = budget.view()
        last_scope["resume"] = encode_resume("thread", self.client.config.heybox_id,
                {"binding": binding, "protocol": self.client.config.protocol_mode, "phase": phase,
                 "page": page if phase == "comments" else root_page, "cursor": cursor}) if resumable and last_scope["completion"] != "complete" else None
        data = {"post_id": args.link_id, "root_id": args.root_id, "target_id": target, "target_observed": target in nodes,
                "target_observed_this_read": target in observed,
                "reply_location": reply_location,
                "ancestor_ids": ancestors, "missing_ancestor_ids": missing, "unknown_parent_ids": unknown_parents,
                "ancestry_complete": bool(ancestors) and ancestors[-1] == args.root_id and not missing and not unknown_parents,
                "items": list(nodes.values())}
        if args.raw:
            data["raw_pages"] = raw_pages
        return data, last_scope
