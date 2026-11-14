"""Community discovery and bounded platform reading."""
from __future__ import annotations

import argparse

from documents import author, comment, document, floors, identifier, integer, link_id, result_object, source, summary
from paging import Budget, ReadLimit, coverage, decode_resume, encode_resume
from xhh_sdk.exceptions import XhhConfigError

COMMANDS = {"community", "topics", "hot", "feed", "search", "post", "comments", "replies"}


def bounded(value):
    number = int(value)
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("must be within 1..100")
    return number


def register(commands):
    for name in sorted(COMMANDS):
        parser = commands.add_parser(name)
        parser.add_argument("--raw", action="store_true")
        parser.add_argument("--requests", type=bounded, default=10)
        parser.add_argument("--seconds", type=bounded, default=30)
        parser.add_argument("--pages", type=bounded, default=1)
        parser.add_argument("--resume")
        if name == "community":
            parser.add_argument("action", choices=("list", "search", "info", "children"))
            parser.add_argument("target", nargs="?")
        elif name == "topics":
            parser.add_argument("query")
        elif name in ("post", "comments"):
            parser.add_argument("link_id", type=link_id)
            parser.add_argument("--page", type=bounded, default=1)
            parser.add_argument("--limit", type=bounded, default=50)
            parser.add_argument("--sort", choices=("hot", "time_aes", "time_desc"), default="hot")
        elif name == "replies":
            parser.add_argument("root_id", type=link_id)
            parser.add_argument("--post-id", type=link_id)
            parser.add_argument("--cursor")
        else:
            parser.add_argument("--offset", type=int, default=0)
            parser.add_argument("--limit", type=bounded, default=20)
            if name == "feed":
                parser.add_argument("topic_id", type=link_id)
                parser.add_argument("--sort", choices=("platform", "created", "comments", "charges"), default="platform")
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


class Browser:
    def __init__(self, client):
        self.client = client

    def fetch(self, route, query, budget):
        raw = budget.call(self.client, lambda: self.client.transport.signed_request(route, query=query))
        return raw, result_object(raw), source(self.client, route)

    def execute(self, args):
        budget = Budget(args.requests, args.seconds)
        if args.resume:
            raise XhhConfigError("resume is not available for this command yet")
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
        elif args.command in ("post", "comments"):
            raw, result, evidence = self.fetch("/bbs/app/link/tree",
                         {"link_id": args.link_id, "page": str(args.page), "limit": str(args.limit), "sort_filter": args.sort}, budget)
            if not isinstance(result.get("link"), dict):
                raise XhhConfigError("post missing from platform result")
            nodes = floors(result.get("comments"), args.link_id)
            data = document(result["link"], evidence) if args.command == "post" else {"post_id": args.link_id, "items": nodes, "source": evidence}
            scope = coverage(result, count=len(result["comments"]), position=args.page, mode="page")
            scope["sort"] = args.sort
            if args.command == "post":
                scope["body"] = data["body"]["completeness"]
                scope["gaps"].extend(data["body"]["gaps"])
            data["available_sorts"] = result.get("sort_filter", [])
        elif args.command == "replies":
            query = {"root_comment_id": args.root_id, "hide_cy": "0", "h_src": ""}
            if args.cursor is not None:
                query["lastval"] = args.cursor
            raw, result, evidence = self.fetch("/bbs/app/comment/sub/comments", query, budget)
            nodes = rows(result, ("comments",))
            data = {"items": [comment(node, args.post_id, args.root_id) for node in nodes], "source": evidence,
                    "root_id": args.root_id, "post_id": args.post_id}
            scope = coverage(result, count=len(nodes), position=args.cursor, mode="cursor")
        else:
            specs = {"feed": ("/bbs/app/topic/feeds", {"topic_id": getattr(args, "topic_id", ""), "h_src": ""}),
                     "hot": ("/bbs/app/hot_news/main_list", {}),
                     "search": ("/bbs/app/api/general/search/v1", {"q": getattr(args, "query", ""),
                                "search_type": getattr(args, "type", "link"), "request_source": "edit_link"})}
            route, query = specs[args.command]
            raw, result, evidence = self.fetch(route, {**query, "offset": str(args.offset), "limit": str(args.limit)}, budget)
            records = rows(result, ("links", "search_result", "list", "news"))
            items = [summary(row) for row in records] if args.command != "search" or args.type == "link" else records
            scope = coverage(result, count=len(records), position=args.offset)
            if args.command == "feed":
                items = [item for item in items if (args.content_type == "all" or item["content_type"] == args.content_type)
                         and (args.after is None or item["created_at"] is not None and item["created_at"] >= args.after)
                         and (item["statistics"]["comments"] or 0) >= args.min_comments]
                if args.sort != "platform":
                    items.sort(key=lambda item: item["created_at"] or 0 if args.sort == "created"
                               else item["statistics"][args.sort] or 0, reverse=True)
                scope.update(selected=len(items), excluded=len(records) - len(items), sorting_scope="returned_items", sort=args.sort)
            data = {"items": items, "source": evidence, "navigation": {"sorts": result.get("sort_filter"), "filters": result.get("filters")}}
        if args.raw:
            data["raw"] = raw
        scope["budget"] = budget.view()
        return data, scope
