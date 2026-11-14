"""Community discovery and bounded platform reading."""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.parse
import urllib.request

from documents import author, comment, document, floors, identifier, integer, link_id, result_object, source, summary, entity_id, enrich_observation
from paging import Budget, ReadLimit, coverage, decode_resume, encode_resume
from xhh_sdk.exceptions import XhhConfigError, XhhError
from xhh_sdk.web_signer import sign_web_request

COMMANDS = {"community", "topics", "hot", "feed", "search", "post", "comments", "replies", "thread", "user", "favourites"}


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
        elif name in ("user", "favourites"):
            parser.add_argument("--offset", type=int, default=0)
            parser.add_argument("--limit", type=bounded, default=20)
            if name == "user":
                parser.add_argument("user_id", type=numeric_argument)
                parser.add_argument("--view", choices=("profile", "posts", "followers", "following"), default="profile")
            else:
                parser.add_argument("action", choices=("folders", "posts"))
                parser.add_argument("folder_id", nargs="?")
        elif name in ("post", "comments", "thread"):
            parser.add_argument("link_id", type=post_argument)
            parser.add_argument("--page", type=page_index, default=1)
            parser.add_argument("--limit", type=bounded, default=50)
            parser.add_argument("--sort", choices=("hot", "time_aes", "time_desc"), default="hot")
            if name == "post":
                parser.add_argument("--reader", choices=("native", "website"), default="native")
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
                parser.add_argument("--order", choices=("recommended", "newest", "recent-replies"), help="platform feed order; --sort only arranges retrieved items")
                parser.add_argument("--content-type", choices=("all", "article", "dynamic"), default="all")
                parser.add_argument("--after", type=int)
                parser.add_argument("--min-comments", type=int, default=0)
            if name == "search":
                parser.add_argument("query")
                parser.add_argument("--type", choices=("link", "game", "user"), default="link")


def rows(result, keys):
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in keys:
            if isinstance(result.get(key), list):
                return result[key]
    raise XhhConfigError("expected list not present in platform result")


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
    def __init__(self, client):
        self.client = client
        self.identity_verified = False

    def fetch(self, route, query, budget):
        raw = budget.call(self.client, lambda: self.client.transport.signed_request(route, query=query))
        if route == "/bbs/app/profile/user/link/list" and isinstance(raw.get("post_links"), list):
            result = {"links": raw["post_links"], "user": raw.get("user"), "has_more": raw.get("has_more")}
        else:
            result = result_object(raw)
        evidence = source(self.client, route)
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
        if args.command == "thread":
            return self.thread(args, budget, prior_data)
        paged = (args.command in ("comments", "replies", "feed", "hot", "search")
                 or args.command == "user" and args.view != "profile"
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
                scope["error"] = {"type": type(error).__name__, "status": getattr(error, "status", None)}
                break
            data = page
            if paged:
                observed_positions.append(position)
            if args.raw and "raw" in page:
                raw_pages.append(page.pop("raw"))
            scope = current
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
            if args.command == "feed" and args.sort != "platform":
                items.sort(key=lambda item: item["created_at"] or 0 if args.sort == "created"
                           else item["statistics"][args.sort] or 0, reverse=True)
            data["items"] = items
            scope["duplicates_removed"] = duplicates
            scope["returned_total"] = returned_total
            if args.command == "feed":
                scope.update(selected=len(items), excluded=excluded_total)
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

    def website(self, query, budget):
        if self.client.config.protocol_mode != "web":
            raise XhhConfigError("website reader requires an explicitly selected Web session")
        route = "/bbs/app/link/tree"
        signature = sign_web_request(route)
        parameters = {"client_type": "web", "x_client_type": "web", "os_type": "web", "x_os_type": "Windows", "x_app": "heybox_website",
                      "version": "999.0.4", "web_version": "2.5", "owner_only": "1",
                      "heybox_id": self.client.config.heybox_id, **{key: value for key, value in signature.items() if key != "_rnd"}, **query}
        request = urllib.request.Request(self.client.config.api_base.rstrip("/") + route + "?" + urllib.parse.urlencode(parameters),
                    headers={"Cookie": self.client.config.cookie_web, "Referer": self.client.config.web_origin + "/",
                             "User-Agent": self.client.config.user_agent_web}, method="GET")
        transport = self.client.transport
        raw = budget.call(self.client, lambda: transport._checked_response(route, transport._decode(transport._open(request))))
        evidence = source(self.client, route)
        evidence["identity_verified"] = self.identity_verified
        evidence["reader"] = "website"
        return raw, result_object(raw), evidence

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
                if author(returned_user)["uid"] != args.user_id:
                    raise XhhConfigError("returned author identity does not match requested user")
                if args.view == "posts":
                    records = rows(result, ("links",))
                    data = {"user_id": args.user_id, "items": [summary(row) for row in records], "source": evidence}
                    scope = coverage(result, count=len(records), position=args.offset)
                else:
                    user = returned_user
                    data = {**author(user), "bio": user.get("signature") or user.get("description"), "source": evidence,
                            "statistics": {"followers": integer(user.get("fan_num")), "following": integer(user.get("follow_num")),
                                           "posts": integer(user.get("post_link_num")), "articles": integer(user.get("post_article_num"))}}
                    scope = {"completion": "unknown", "gaps": [], "full_history": False}
            else:
                route = "/bbs/app/profile/" + ("follower/list" if args.view == "followers" else "following/list")
                raw, result, evidence = self.fetch(route, {"userid": args.user_id, "offset": str(args.offset), "limit": str(args.limit)}, budget)
                records = rows(result, ("followers", "following", "users", "list"))
                data = {"items": [author(row) for row in records], "user_id": args.user_id, "source": evidence}
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
        elif args.command in ("post", "comments"):
            query = {"link_id": args.link_id, "page": str(args.page), "limit": str(args.limit), "sort_filter": args.sort}
            raw, result, evidence = self.website(query, budget) if getattr(args, "reader", "native") == "website" else self.fetch("/bbs/app/link/tree", query, budget)
            if args.command == "post" and not isinstance(result.get("link"), dict):
                raise XhhConfigError("post missing from platform result")
            nodes = floors(result.get("comments"), args.link_id) if args.command == "comments" else []
            data = document(result["link"], evidence) if args.command == "post" else {"post_id": args.link_id, "items": nodes, "source": evidence}
            scope = coverage(result, count=len(result.get("comments", [])), position=args.page, mode="page")
            if "comments" not in result:
                scope["gaps"].append("comments_not_returned")
            scope["sort"] = args.sort
            if args.command == "post":
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
            specs = {"feed": ("/bbs/app/topic/feeds", {"topic_id": getattr(args, "topic_id", ""), "h_src": ""}),
                     "hot": ("/bbs/app/hot_news/main_list", {}),
                     "search": ("/bbs/app/api/general/search/v1", {"q": getattr(args, "query", ""),
                                "search_type": getattr(args, "type", "link"), "request_source": "edit_link"})}
            route, query = specs[args.command]
            if args.command == "feed" and args.order:
                query["sort_filter"] = {"recommended": "hot-rank", "newest": "create", "recent-replies": "reply"}[args.order]
            raw, result, evidence = self.fetch(route, {**query, "offset": str(args.offset), "limit": str(args.limit)}, budget)
            records = rows(result, ("links", "search_result", "items", "list", "news"))
            items = [summary(row) for row in records] if args.command != "search" or args.type == "link" else records
            scope = coverage(result, count=len(records), position=args.offset)
            if args.command == "feed":
                items = [item for item in items if (args.content_type == "all" or item["content_type"] == args.content_type)
                         and (args.after is None or item["created_at"] is not None and item["created_at"] >= args.after)
                         and (item["statistics"]["comments"] or 0) >= args.min_comments]
                scope.update(selected=len(items), excluded=len(records) - len(items), sorting_scope="returned_items", sort=args.sort,
                             platform_order=args.order or "default")
            data = {"items": items, "source": evidence, "navigation": {"sorts": result.get("sort_filter", result.get("sort_filter_list")),
                    "filters": result.get("filters", result.get("filter_list")), "time_ranges": result.get("time_range_list")}}
        if args.raw:
            data["raw"] = raw
        return data, scope

    def thread(self, args, budget, prior_data=None):
        nodes, root, page, cursor = {}, None, args.page, None
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
                last_scope["error"] = {"type": type(error).__name__, "status": getattr(error, "status", None)}
                break
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
        last_scope["continuation"] = {"command": phase, "post_id": args.link_id, "root_id": args.root_id,
                                      "page": page if phase == "comments" else None, "cursor": cursor} if resumable and last_scope["completion"] != "complete" else None
        last_scope["budget"] = budget.view()
        last_scope["resume"] = encode_resume("thread", self.client.config.heybox_id,
                {"binding": binding, "protocol": self.client.config.protocol_mode, "phase": phase,
                 "page": page if phase == "comments" else root_page, "cursor": cursor}) if resumable and last_scope["completion"] != "complete" else None
        data = {"post_id": args.link_id, "root_id": args.root_id, "target_id": target, "target_observed": target in nodes,
                "target_observed_this_read": target in observed,
                "ancestor_ids": ancestors, "missing_ancestor_ids": missing, "unknown_parent_ids": unknown_parents,
                "ancestry_complete": bool(ancestors) and ancestors[-1] == args.root_id and not missing and not unknown_parents,
                "items": list(nodes.values())}
        if args.raw:
            data["raw_pages"] = raw_pages
        return data, last_scope
