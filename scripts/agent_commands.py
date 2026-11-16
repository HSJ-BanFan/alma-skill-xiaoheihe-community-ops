from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request
from html.parser import HTMLParser

from emoji_catalog import Catalog, EmojiError, parse_creator_assets, save_catalog
from emoji_preview import preview
import messages
import sticker_store
from xhh_sdk.exceptions import XhhConfigError

ROOTS = {"describe", "emojis", "stickers", "compose", "send", "verify"}
OFFLINE_COMMANDS = {"preview-post"}
AUTH_ROOTS = {"send", "verify"}
READ_COMMANDS = {"post", "comments", "thread", "feed", "inbox", "interaction", "creator-options"}
CREATOR_URL = "https://creator.xiaoheihe.cn/creator"


def _limit(value):
    number = int(value)
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("limit must be 1..100")
    return number


def _offset(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("offset must be nonnegative")
    return number


def _draft_json(value):
    return json.loads(value)


def _prepared_json(value):
    return json.loads(value)


_draft_json.input_schema = messages.draft_schema()
_prepared_json.input_schema = messages.prepared_schema()


def _emoji_filters(parser, *, listing=False):
    parser.add_argument("--limit", type=_limit, default=20 if listing else 5)
    parser.add_argument("--usage", choices=("post", "comment", "reply"))
    parser.add_argument("--avoid", action="append", default=[], help="exclude caution tags such as sarcasm or anger")
    parser.add_argument("--offset", type=_offset, default=0)


def register(commands):
    parser = commands.add_parser("describe", help="discover CLI commands and JSON schemas; offline, no account required")
    parser.add_argument("--command", dest="described_command", help="return one command schema, for example compose or 'emojis get'")
    parser.set_defaults(agent_action="describe")
    parser = commands.add_parser("emojis", help="offline native catalog, meaning notes, preview and official-source refresh")
    parser.set_defaults(agent_action="emojis.list", limit=20, usage=None, avoid=[])
    subcommands = parser.add_subparsers(dest="emoji_action")
    listing = subcommands.add_parser("list", help="list a bounded page of the validated native catalog")
    _emoji_filters(listing, listing=True)
    listing.set_defaults(agent_action="emojis.list")
    search = subcommands.add_parser("search", help="find native emojis by name, intent and meaning notes")
    search.add_argument("query")
    _emoji_filters(search)
    search.set_defaults(agent_action="emojis.search")
    inspect = subcommands.add_parser("get", help="inspect an exact native emoji ID and optionally produce an image preview")
    inspect.add_argument("emoji_id")
    inspect.add_argument("--preview", action="store_true", help="download the official sprite and return a labelled PNG crop")
    inspect.set_defaults(agent_action="emojis.get")
    refresh = subcommands.add_parser("refresh", help="refresh the catalog from public official Creator assets; preserves cache on failure")
    refresh.add_argument("--seconds", type=_limit, default=30)
    refresh.set_defaults(agent_action="emojis.refresh")
    parser = commands.add_parser("stickers", help="register and inspect caller-created image stickers; requires Pillow for import")
    subcommands = parser.add_subparsers(dest="sticker_action", required=True)
    add = subcommands.add_parser("import", help="import an explicit PNG/JPEG/GIF and return its content-addressed sticker ID")
    add.add_argument("image_file", type=Path)
    add.add_argument("--caption")
    add.add_argument("--meaning")
    add.add_argument("--tag", dest="tags", action="append", default=[])
    add.set_defaults(agent_action="stickers.import")
    inspect = subcommands.add_parser("get", help="inspect and verify an imported image sticker")
    inspect.add_argument("sticker_id")
    inspect.set_defaults(agent_action="stickers.get")
    for action in ("list", "search"):
        parser = subcommands.add_parser(action, help="query imported sticker captions, meaning notes and tags")
        if action == "search":
            parser.add_argument("query")
        parser.add_argument("--limit", type=_limit, default=20)
        parser.add_argument("--offset", type=_offset, default=0)
        parser.set_defaults(agent_action="stickers." + action)
    parser = commands.add_parser("compose", help="validate typed content and prepare a message locally; never sends")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("spec_file", nargs="?", type=Path, help="UTF-8 typed message JSON")
    source.add_argument("--spec-json", dest="spec", type=_draft_json, help="typed message object for programmatic callers")
    parser.add_argument("--out", type=Path, help="write the prepared object to this explicit output file")
    parser.add_argument("--preview-file", type=Path, help="write a self-contained HTML preview; fetches official sprites if needed")
    parser.add_argument("--dry-run", action="store_true", help="explicit local preparation; compose always has no platform writes")
    parser.set_defaults(agent_action="compose")
    parser = commands.add_parser("send", help="send a prepared message with account, target and operation-id validation")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("prepared_file", nargs="?", type=Path)
    source.add_argument("--prepared-json", dest="prepared", type=_prepared_json)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--public", action="store_true", help="public post; omitted saves a post draft")
    parser.set_defaults(agent_action="send")
    parser = commands.add_parser("verify", help="read back a stored operation and compare content; visibility and native render remain separate")
    parser.add_argument("operation_id")
    parser.add_argument("--pages", type=_limit, default=3)
    parser.add_argument("--requests", type=_limit, default=10)
    parser.add_argument("--seconds", type=_limit, default=30)
    parser.set_defaults(agent_action="verify")


class _CreatorAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts, self.styles = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.scripts.append(attrs["src"])
        elif tag == "link" and "stylesheet" in (attrs.get("rel") or "") and attrs.get("href"):
            self.styles.append(attrs["href"])


def refresh_catalog(data_dir, *, seconds=30):
    from network_io import read_http, NetworkDeadline

    import hashlib
    from datetime import datetime, timezone
    import time

    deadline = time.monotonic() + seconds

    def fetch(url):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in ("creator.xiaoheihe.cn", "static.max-c.com")
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise EmojiError("invalid_source", "Creator refresh source must be an official HTTPS host")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EmojiError("refresh_timeout", "Creator refresh deadline exceeded")
        try:
            data = read_http(Request(url, headers={"User-Agent": "xiaoheihe-tools-catalog/1.0"}), remaining, profile="api")
        except NetworkDeadline:
            raise EmojiError("refresh_timeout", "Creator refresh deadline exceeded") from None
        if len(data) > 5 * 1024 * 1024:
            raise EmojiError("invalid_source", "Creator source exceeds the catalog size limit")
        return data

    entry = fetch(CREATOR_URL).decode("utf-8")
    parsed = _CreatorAssets()
    parsed.feed(entry)
    prefix = "https://static.max-c.com/static/heybox/webapp/@heybox-spa/creator/app/"
    js_urls = [urljoin(CREATOR_URL, path) for path in parsed.scripts if urljoin(CREATOR_URL, path).startswith(prefix + "index-")]
    css_urls = [urljoin(CREATOR_URL, path) for path in parsed.styles if urljoin(CREATOR_URL, path).startswith(prefix + "index-")]
    if len(js_urls) != 1 or len(css_urls) != 1:
        raise EmojiError("invalid_source", "Creator entry did not identify one main script and stylesheet")
    js, css = fetch(js_urls[0]), fetch(css_urls[0])
    source = {"kind": "official_creator_assets", "js_url": js_urls[0], "css_url": css_urls[0],
              "js_sha256": hashlib.sha256(js).hexdigest(), "css_sha256": hashlib.sha256(css).hexdigest(),
              "observed_at": datetime.now(timezone.utc).isoformat(), "render_contract": "creator_span_and_shortcode",
              "live_post_verified": False}
    snapshot = parse_creator_assets(js.decode("utf-8"), css.decode("utf-8"), source)
    try:
        previous = Catalog(data_dir=data_dir)
    except EmojiError:
        previous = Catalog()
        source["previous_cache_state"] = "invalid"
    for item in snapshot["items"]:
        try:
            prior = previous.get(item["id"])
        except EmojiError:
            continue
        if prior["name"] == item["name"] and prior["sprite"] == item["sprite"]:
            for key in ("visual_description", "meaning", "tags", "cautions", "annotation_source", "annotation_status"):
                if key in prior:
                    item[key] = prior[key]
    return save_catalog(snapshot, data_dir)


def execute(args, *, client=None):
    action = args.agent_action
    if action == "describe":
        return describe(args.data_dir, command=args.described_command), {}
    if action == "emojis.refresh":
        return refresh_catalog(args.data_dir, seconds=args.seconds), {"source": "official_creator_assets", "live_post_verified": False}
    catalog = Catalog(data_dir=args.data_dir)
    if action in ("emojis.list", "emojis.search"):
        result = catalog.search(getattr(args, "query", ""), usage=args.usage, limit=args.limit, avoid=args.avoid, offset=getattr(args, "offset", 0))
        return result, {"catalog_scope": "validated_snapshot", "live_post_verified": False}
    if action == "emojis.get":
        item = catalog.get(args.emoji_id)
        if args.preview:
            item["preview"] = preview(catalog, [item["id"]], args.data_dir)
        return {"item": item, **catalog.info()}, {"native_render_scope": "local_preview" if args.preview else "not_checked"}
    if action == "stickers.import":
        return sticker_store.register(args.image_file, args.data_dir, caption=args.caption, meaning=args.meaning, tags=args.tags), {"platform_upload": False}
    if action == "stickers.get":
        return sticker_store.get(args.sticker_id, args.data_dir), {"platform_upload": False}
    if action in ("stickers.list", "stickers.search"):
        return sticker_store.search(args.data_dir, getattr(args, "query", ""), limit=args.limit, offset=args.offset), {}
    if action == "compose":
        spec = args.spec if args.spec is not None else json.loads(args.spec_file.read_text(encoding="utf-8-sig"))
        prepared = messages.compose(spec, args.data_dir, protocol=args.protocol or "web", catalog=catalog)
        if args.preview_file:
            from message_preview import write
            prepared["html_preview"] = write(prepared, args.data_dir, args.preview_file, catalog=catalog)
        if args.out:
            args.out.write_text(json.dumps(prepared, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return prepared, {"platform_write": False, "prepared_file": str(args.out.resolve()) if args.out else None}
    if action == "send":
        prepared = args.prepared if args.prepared is not None else json.loads(args.prepared_file.read_text(encoding="utf-8-sig"))
        return messages.send(client, prepared, args.data_dir, args.operation_id, confirm=args.confirm, public=args.public), {"public_visibility": "not_checked"}
    if action == "verify":
        return messages.verify(client, args.operation_id, args.data_dir, pages=args.pages, requests=args.requests, seconds=args.seconds), {}
    raise XhhConfigError("unknown agent action")


def _schema(action):
    converter = action.type
    if getattr(converter, "input_schema", None):
        result = copy.deepcopy(converter.input_schema)
    elif isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        result = {"type": "boolean"}
    elif converter in (int, _limit, _offset) or getattr(converter, "__name__", "") in ("bounded", "page_index", "nonnegative", "positive"):
        result = {"type": "integer"}
    else:
        result = {"type": "string"}
    if action.choices:
        result["enum"] = list(action.choices)
    if isinstance(action, argparse._AppendAction):
        result = {"type": "array", "items": result}
    if action.dest in ("confirm", "env"):
        result["const"] = True
    if converter is _limit or getattr(converter, "__name__", "") in ("bounded", "positive"):
        result.update(minimum=1, maximum=100)
    elif converter is _offset or getattr(converter, "__name__", "") == "nonnegative":
        result["minimum"] = 0
    elif getattr(converter, "__name__", "") == "page_index":
        result["minimum"] = 1
    if action.help not in (None, argparse.SUPPRESS):
        result["description"] = " ".join(value for value in (result.get("description"), action.help) if value)
    if (action.default is not None and action.default != argparse.SUPPRESS
            and isinstance(action.default, (str, bool, int, list)) and not (result.get("const") is True and action.default is False)):
        result["default"] = action.default
    return result


def output_schema(path):
    data = {"type": "object"}
    if path == ("compose",):
        data = messages.prepared_schema()
        data["properties"].update(preview={"type": "object"}, expected={"type": "object"},
                                  dry_run={"type": "boolean", "const": True}, public_visibility={"const": "not_checked"})
        data["required"] += ["preview", "expected", "dry_run", "public_visibility"]
    elif path == ("verify",):
        data = {"type": "object", "properties": {"status": {"enum": ["matched", "mismatch", "unverified"]},
                "operation": {"type": "object"}, "native_render": {"type": "object"},
                "public_visibility": {"const": "not_checked"}},
                "required": ["status", "operation", "native_render", "public_visibility"]}
    return {"type": "object", "properties": {"version": {"type": "integer", "enum": [1, 2]},
            "ok": {"type": "boolean"}, "kind": {"const": path[0]}, "data": {}, "coverage": {"type": "object"},
            "error": {"anyOf": [{"type": "null"}, {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}]}},
            "required": ["version", "ok", "kind", "data", "coverage", "error"], "additionalProperties": False,
            "allOf": [{"if": {"properties": {"ok": {"const": True}}},
                       "then": {"properties": {"data": data, "error": {"type": "null"}}}}]}


def command_definitions():
    import xhh

    root = xhh.parser()
    global_actions = [action for action in root._actions if action.dest in ("account", "config", "env", "protocol", "data_dir")]
    commands = []

    def visit(parser, path, help_text=None):
        subparsers = next((action for action in parser._actions if isinstance(action, argparse._SubParsersAction)), None)
        if subparsers:
            help_by_name = {choice.dest: choice.help for choice in subparsers._choices_actions}
            for name, child in subparsers.choices.items():
                if path or name in ROOTS | READ_COMMANDS | OFFLINE_COMMANDS:
                    visit(child, (*path, name), help_by_name.get(name))
            return
        auth = path[0] in AUTH_ROOTS | READ_COMMANDS
        actions = [*global_actions, *[action for action in parser._actions if action.dest != "help"]]
        actions = [action for action in actions if auth or action.dest not in ("account", "config", "env")]
        fields = {action.dest: _schema(action) for action in actions}
        required = [action.dest for action in actions if action.required or action.dest == "confirm" or
                    not action.option_strings and action.nargs not in ("?", "*")]
        schema = {"type": "object", "properties": fields, "required": list(dict.fromkeys(required)), "additionalProperties": False}
        if path == ("compose",):
            fields["protocol"].update(default="web", description="Local preparation defaults to web; send uses the same explicit protocol.")
        groups = list(parser._mutually_exclusive_groups)
        if auth:
            groups = [*root._mutually_exclusive_groups, *groups]
        constraints = []
        for group in groups:
            names = [action.dest for action in group._group_actions if action.dest in fields]
            if not names:
                continue
            branches = [{"required": [name], "not": {"anyOf": [{"required": [other]} for other in names if other != name]}} for name in names]
            if not group.required and not (auth and set(names) == {"account", "config", "env"}):
                branches.append({"not": {"anyOf": [{"required": [name]} for name in names]}})
            constraints.append({"oneOf": branches})
        if constraints:
            schema["allOf"] = constraints
        parameters = {action.dest: {"flags": action.option_strings,
                      "global": action in global_actions,
                      "encoding": "json" if getattr(action.type, "input_schema", None) else "value",
                      "repeatable": isinstance(action, argparse._AppendAction)} for action in actions}
        commands.append({"name": " ".join(path), "argv": list(path),
                         "description": help_text or parser.description or "Read Xiaoheihe " + " ".join(path) + " with an explicit account.",
                         "input_schema": schema, "output_schema": output_schema(path),
                         "parameters": parameters, "requires_account": auth})
    visit(root, ())
    return commands


def describe(data_dir=None, *, command=None):
    catalog = Catalog(data_dir=data_dir)
    commands = command_definitions()
    if command:
        commands = [definition for definition in commands if definition["name"] == command]
        if not commands:
            raise XhhConfigError("command name is not in describe output")
    else:
        commands = [{key: definition[key] for key in ("name", "argv", "description", "requires_account")}
                    for definition in commands]
    return {"schema_version": 1, "name": "xiaoheihe-tools", "entrypoint_file": str(Path(__file__).with_name("xhh.py").resolve()),
            "runtime": {"python": ">=3.10", "core_dependencies": [], "media_dependency": "Pillow"},
            "catalog": catalog.info(), "commands": commands, "schema_lookup": "describe --command <name>",
            "workflow": ["emojis search/get", "compose", "send with account and operation_id", "verify"],
            "semantics": {"native_emoji": "Official directory ID; post spans and comment/reply shortcodes are encoded by the tool.",
                          "image_sticker": "Caller-created PNG/JPEG/GIF imported locally; post image or comment attachment, not reply.",
                          "rank_score": "Keyword ranking, not a probability.", "verification": "Receipt, readback match, public visibility and client icon render are separate."}}
