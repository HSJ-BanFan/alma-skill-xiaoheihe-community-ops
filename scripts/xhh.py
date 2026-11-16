#!/usr/bin/env python3
"""Self-contained Xiaoheihe tools. Python 3.10+, no host integration."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from operations import Journal, OperationError, numeric_id, prepare_write, read, verify_identity
from xhh_sdk.accounts import AccountStore
from xhh_sdk.client import XhhClient
from xhh_sdk.config import XhhConfig
from xhh_sdk.exceptions import XhhError, XhhConfigError
from documents import link_id
from interaction_history import InteractionHistory, Scope
from paging import Budget, ReadLimit
from interaction_reads import capture_private, synchronize
import browser
import agent_commands
from emoji_catalog import EmojiError
from messages import MessageError

WRITES = {"comment", "reply", "like", "favourite", "unfavourite", "delete-comment", "delete-post", "upload", "publish", "send"}


def data_root() -> Path:
    return Path(os.environ.get("XHH_DATA_DIR") or (Path.home() / ".xiaoheihe-tools")).expanduser()


def positive(value):
    number = int(value)
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("must be between 1 and 100")
    return number


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    source = result.add_mutually_exclusive_group()
    source.add_argument("--account", help="explicit account alias; no main/alt policy")
    source.add_argument("--config", type=Path, help="explicit credential JSON")
    source.add_argument("--env", action="store_true", help="read XHH_PKEY and XHH_HEYBOX_ID")
    result.add_argument("--protocol", choices=("app", "web"))
    result.add_argument("--data-dir", type=Path, default=data_root())
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("account-info", "operation"):
        command = commands.add_parser(name)
        if name == "operation":
            command.add_argument("operation_id")
    command = commands.add_parser("creator-options")
    command.add_argument("--link-id", type=numeric_id)
    command = commands.add_parser("asset", help="download platform image and create local preview; no account required")
    command.add_argument("url")
    command.add_argument("--refresh", action="store_true")
    command.add_argument("--seconds", type=positive, default=20)
    command = commands.add_parser("snapshot", help="read or resume an account-scoped public browsing snapshot")
    command.add_argument("action", choices=("list", "show", "diff", "resume"))
    command.add_argument("name", nargs="?")
    command.add_argument("other", nargs="?")
    for name in ("inbox", "drafts", "my-posts", "my-comments"):
        command = commands.add_parser(name)
        command.add_argument("--offset", type=nonnegative, default=0)
        command.add_argument("--limit", type=positive, default=20)
        if name in ("inbox", "my-comments"):
            command.add_argument("--requests", type=positive, default=10)
            command.add_argument("--seconds", type=positive, default=30)
    command = commands.add_parser("interaction", help="local interaction evidence and bounded read-only synchronization")
    actions = command.add_subparsers(dest="action", required=True)
    for name in ("status", "history", "sync"):
        action = actions.add_parser(name)
        action.add_argument("link_id", type=browser.post_argument)
        action.add_argument("--peer", type=browser.numeric_argument)
        action.add_argument("--root", type=browser.numeric_argument)
        if name in ("history", "sync"):
            action.add_argument("--limit", type=positive, default=20)
            action.add_argument("--resume")
        if name == "sync":
            action.add_argument("--pages", type=positive, default=10)
            action.add_argument("--requests", type=positive, default=10)
            action.add_argument("--seconds", type=positive, default=30)
            action.add_argument("--sort", choices=("hot", "time_aes", "time_desc"), default="time_aes")
    browser.register(commands)
    agent_commands.register(commands)
    for name in sorted(WRITES - {"send"}):
        command = commands.add_parser(name)
        command.add_argument("--confirm", action="store_true")
        command.add_argument("--operation-id", required=True)
        if name not in ("upload", "publish"):
            command.add_argument("link_id", type=numeric_id)
        if name in ("comment", "reply"):
            command.add_argument("--text-file", type=Path, required=True)
        if name == "reply":
            command.add_argument("root_id", type=numeric_id)
            command.add_argument("reply_id", type=numeric_id)
            command.add_argument("--page", type=browser.page_index, default=1)
            command.add_argument("--limit", type=browser.bounded, default=50)
            command.add_argument("--sort", choices=("hot", "time_aes", "time_desc"), default="hot")
            command.add_argument("--cursor")
        if name == "comment":
            image = command.add_mutually_exclusive_group()
            image.add_argument("--image-file", type=Path)
            image.add_argument("--image-url")
        if name == "delete-comment":
            command.add_argument("comment_id", type=numeric_id)
        if name == "upload":
            command.add_argument("image_file", type=Path)
        if name == "publish":
            command.add_argument("spec_file", type=Path)
            command.add_argument("--public", action="store_true", help="publish publicly; default saves a platform draft")
    for name in ("account", "doctor", "signer"):
        commands.add_parser(name, add_help=False, help="bundled SDK account/runtime command; append --help")
    return result


def load_config(args):
    if args.account:
        config = AccountStore(args.data_dir).get_config(args.account)
    elif args.config:
        config = XhhConfig.from_file(args.config)
    elif args.env:
        config = XhhConfig.from_env()
    else:
        raise OperationError("account_selection_required")
    if args.protocol:
        config.protocol_mode = args.protocol
    config.validate()
    return config


def redact(value, secret=""):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if any(token in key.lower() for token in
                ("pkey", "cookie", "token", "secret", "password", "authorization"))
                else redact(item, secret) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, secret) for item in value]
    if isinstance(value, str) and secret:
        return value.replace(secret, "[REDACTED]")
    return value


def emit(ok, kind, data=None, *, coverage=None, error=None, secret=""):
    version = 2 if kind in browser.COMMANDS or kind in {"asset", "snapshot", "interaction"} | agent_commands.ROOTS else 1
    print(json.dumps(redact({"version": version, "ok": ok, "kind": kind, "data": data,
                     "coverage": coverage or {}, "error": error}, secret), ensure_ascii=False), flush=True)


def acknowledge_delivery(store, uid, name, revision):
    try:
        store.delivered(uid, name, revision)
    except (sqlite3.Error, OSError, XhhConfigError):
        pass


def merge_resumed_coverage(snapshot, coverage, delta):
    coverage["observed_positions"] = list(dict.fromkeys(
        snapshot["coverage"].get("observed_positions", []) + coverage.get("observed_positions", [])))
    coverage["duplicates_removed_since_snapshot"] = len(delta["unchanged"])


def browsing_capture(store, uid, secret):
    def capture(nodes, evidence, post_id, root_id, coverage):
        try:
            if evidence.get("post_id_source") == "caller" and store.root_post(uid, root_id) != post_id:
                return {"persisted": False, "reason": "root_membership_not_established"}
            return store.capture(uid, redact(nodes, secret), "public", evidence,
                                 scope=Scope(uid, post_id, root_id=root_id), coverage=coverage)
        except (sqlite3.Error, OSError, XhhConfigError, ValueError, TypeError) as error:
            return {"persisted": False, "reason": "interaction_cache_incomplete", "error_type": type(error).__name__}
    return capture


def add_interaction_summaries(store, uid, command, data):
    if command not in ("post", "feed", "hot"):
        return
    items = [data] if command == "post" else data["items"]
    try:
        summaries = store.summaries(uid, [item["id"] for item in items])
    except (sqlite3.Error, OSError, XhhConfigError, ValueError, TypeError) as error:
        summaries = {item["id"]: {"account_uid": uid, "participation": "unknown", "history_complete": False,
            "my_known_comment_count": None, "my_publicly_observed_comment_count": None, "completed_round_count": None,
            "count_semantics": "public_verified_lower_bound", "discussion_status": "unknown",
            "relation_completeness": "unknown", "own_reply_relations_complete": None, "unattributed_own_comment_count": None,
            "cache_incomplete": True, "error_type": type(error).__name__} for item in items}
    for item in items:
        item["interaction"] = summaries[item["id"]]


def main(argv=None, *, client_factory=XhhClient):
    argv = list(sys.argv[1:] if argv is None else argv)
    root_parser = parser()
    args, remaining = root_parser.parse_known_args(argv)
    if args.command in ("account", "doctor", "signer"):
        from xhh_sdk import cli
        cli.DEFAULT_CONFIG = args.data_dir / "config.json"
        forwarded = ["--data-dir", str(args.data_dir)]
        for name in ("account", "config", "protocol"):
            value = getattr(args, name)
            if value:
                forwarded.extend(["--" + name, str(value)])
        if args.env:
            forwarded.append("--env")
        return cli.main([*forwarded, args.command, *remaining])
    if remaining:
        root_parser.error("unrecognized arguments: " + " ".join(remaining))
    secret = ""
    try:
        if args.command in WRITES and not args.confirm:
            raise OperationError("confirmation_required")
        if args.command in agent_commands.ROOTS:
            client = None
            if args.command in agent_commands.AUTH_ROOTS:
                config = load_config(args)
                secret = config.pkey
                client = client_factory(config)
            data, coverage = agent_commands.execute(args, client=client)
            emit(True, args.command, data, coverage=coverage, secret=secret)
            return 0
        if args.command == "asset":
            from assets import fetch
            data = fetch(args.url, args.data_dir, refresh=args.refresh, seconds=args.seconds)
            emit(True, args.command, data, coverage={"preview_scope": "first_frame"})
            return 0
        config = load_config(args)
        secret = config.pkey
        interactions = InteractionHistory(args.data_dir)
        if args.command == "interaction":
            scope = Scope(str(config.heybox_id), args.link_id, args.peer, args.root)
            if args.action == "status":
                data = interactions.status(scope)
                coverage = data["coverage"]
            elif args.action == "history":
                data = interactions.history(scope, limit=args.limit, resume=args.resume)
                coverage = data["coverage"]
            else:
                data, coverage = synchronize(interactions, client_factory(config), scope, args, sanitize=lambda value: redact(value, secret))
            ok = coverage.get("stop_reason") not in ("platform_error", "storage_error")
            failure = coverage.get("error") or {}
            error_code = failure.get("code") if not data.get("identity_verified_this_read", True) or coverage.get("stop_reason") == "storage_error" else None
            emit(ok, args.command, data, coverage=coverage,
                 error=None if ok else {"code": error_code or "platform_read_stopped", "retry_allowed": False}, secret=secret)
            return 0 if ok else 1
        if args.command == "snapshot":
            from observations import Store, difference, merge, record_key, SnapshotConflict
            store = Store(args.data_dir)
            if args.action == "list":
                emit(True, args.command, {"items": store.list(config.heybox_id)}, secret=secret)
                return 0
            saved = store.get(config.heybox_id, args.name)
            if args.action == "show":
                emit(True, args.command, saved, secret=secret)
                return 0
            if args.action == "diff":
                other = store.get(config.heybox_id, args.other)
                if saved["query_key"] != other["query_key"]:
                    raise XhhConfigError("snapshots belong to different queries")
                emit(True, args.command, difference(saved["data"], other["data"]), secret=secret)
                return 0
            if saved["pending_response"] is not None:
                pending = saved["pending_response"]
                emit(pending["ok"], args.command, pending["data"], coverage={**pending["coverage"], "replayed_delivery": True}, error=pending.get("error"), secret=secret)
                acknowledge_delivery(store, config.heybox_id, args.name, saved["revision"])
                return 0 if pending["ok"] else 1
            token = saved["coverage"].get("resume")
            if not token:
                raise XhhConfigError("snapshot has no resumable continuation")
            resumed = parser().parse_args([*saved["request"], "--resume", token, "--snapshot", args.name])
            data, coverage = browser.Browser(client_factory(config), capture=browsing_capture(interactions, config.heybox_id, secret)).execute(resumed, verify_account=True, prior_data=saved["data"])
            add_interaction_summaries(interactions, config.heybox_id, resumed.command, data)
            delta = difference(saved["data"], data)
            combined = merge(saved["data"], data)
            merge_resumed_coverage(saved, coverage, delta)
            if isinstance(data.get("items"), list):
                data["items"] = [item for item in data["items"] if record_key(item) not in delta["unchanged"]]
            ok = coverage.get("stop_reason") != "platform_error"
            pending = {"ok": ok, "data": redact(data, secret), "coverage": coverage,
                       "error": None if ok else {"code": "platform_read_stopped", "retry_allowed": False}}
            try:
                revision = store.save(config.heybox_id, args.name, saved["query_key"], redact(combined, secret), coverage,
                                      browser.request_arguments(resumed), expected_revision=saved["revision"], pending_response=pending)
            except SnapshotConflict as error:
                emit(False, args.command, data, coverage=coverage, error={"code": "snapshot_conflict", "current_revision": error.revision}, secret=secret)
                return 1
            emit(coverage.get("stop_reason") != "platform_error", args.command, data, coverage=coverage, error=pending["error"], secret=secret)
            acknowledge_delivery(store, config.heybox_id, args.name, revision)
            return 1 if coverage.get("stop_reason") == "platform_error" else 0
        if args.command == "operation":
            value = Journal(args.data_dir).read(config.heybox_id, args.operation_id)
            emit(value is not None, args.command, value, error=None if value else {"code": "operation_not_found"}, secret=secret)
            return 0 if value else 1
        client = client_factory(config)
        if args.command in WRITES:
            verify_identity(client)
            journal = Journal(args.data_dir)
            payload, send = prepare_write(client, args.command, args,
                before_post=lambda request: journal.bind_submitted_request(config.heybox_id, args.operation_id, request))
            context = {key: getattr(args, key) for key in ("page", "limit", "sort", "cursor") if hasattr(args, key)}
            context["protocol"] = config.protocol_mode
            data = journal.execute(config.heybox_id, args.operation_id, args.command, payload, send,
                                   identity_verified=True, context=context)
            coverage = {"public_visibility": "not_checked"}
        elif args.command in browser.COMMANDS:
            from observations import Store, difference, merge, SnapshotConflict
            store = Store(args.data_dir)
            key = browser.query_key(args) + ":" + config.protocol_mode
            previous = store.get(config.heybox_id, args.since) if args.since else None
            if previous is not None and previous["query_key"] != key:
                raise XhhConfigError("baseline snapshot belongs to a different query")
            existing = store.optional(config.heybox_id, args.snapshot) if args.snapshot else None
            if existing is not None and existing["pending_response"] is not None:
                if existing["query_key"] != key:
                    raise XhhConfigError("snapshot has undelivered data; replay it with snapshot resume before replacing it")
                pending = existing["pending_response"]
                emit(pending["ok"], args.command, pending["data"], coverage={**pending["coverage"], "replayed_delivery": True}, error=pending.get("error"), secret=secret)
                acknowledge_delivery(store, config.heybox_id, args.snapshot, existing["revision"])
                return 0 if pending["ok"] else 1
            if args.resume and existing is not None and existing["query_key"] != key:
                raise XhhConfigError("resumed snapshot belongs to a different query")
            data, coverage = browser.Browser(client, capture=browsing_capture(interactions, config.heybox_id, secret)).execute(args, verify_account=bool(args.snapshot or args.since), prior_data=existing["data"] if args.resume and existing else None)
            add_interaction_summaries(interactions, config.heybox_id, args.command, data)
            if previous is not None:
                coverage["delta"] = difference(previous["data"], data)
            if args.snapshot:
                combined = merge(existing["data"], data) if args.resume and existing else data
                if args.resume and existing:
                    merge_resumed_coverage(existing, coverage, difference(existing["data"], data))
                pending = {"ok": coverage.get("stop_reason") != "platform_error", "data": redact(data, secret), "coverage": coverage,
                           "error": {"code": "platform_read_stopped", "retry_allowed": False} if coverage.get("stop_reason") == "platform_error" else None}
                try:
                    revision = store.save(config.heybox_id, args.snapshot, key, redact(combined, secret), coverage,
                            browser.request_arguments(args), expected_revision=existing["revision"] if existing else 0, pending_response=pending)
                except SnapshotConflict as error:
                    emit(False, args.command, data, coverage=coverage, error={"code": "snapshot_conflict", "current_revision": error.revision}, secret=secret)
                    return 1
            if coverage.get("stop_reason") == "platform_error":
                emit(False, args.command, data, coverage=coverage, error={"code": "platform_read_stopped", "retry_allowed": False}, secret=secret)
                if args.snapshot:
                    acknowledge_delivery(store, config.heybox_id, args.snapshot, revision)
                return 1
        elif args.command in ("inbox", "my-comments"):
            budget = Budget(args.requests, args.seconds)
            budget.call(client, lambda: verify_identity(client))
            data, coverage = budget.call(client, lambda: read(client, args.command, args))
            coverage.update(identity_verified_this_read=True, budget=budget.view())
            try:
                coverage["interaction_capture"] = capture_private(interactions, client, args.command, data, coverage,
                    sanitize=lambda value: redact(value, secret))
            except (sqlite3.Error, OSError, XhhConfigError) as error:
                coverage["interaction_capture"] = {"persisted": False, "reason": "interaction_cache_incomplete", "error_type": type(error).__name__}
        else:
            data, coverage = read(client, args.command, args)
            if args.command == "account-info":
                interactions.establish(config.heybox_id, {"source": "account_info_identity_check", "protocol": config.protocol_mode})
        emit(True, args.command, data, coverage=coverage, secret=secret)
        if args.command in browser.COMMANDS and args.snapshot:
            acknowledge_delivery(store, config.heybox_id, args.snapshot, revision)
        return 0
    except BrokenPipeError:
        return 1
    except OperationError as error:
        emit(False, args.command, {"operation": error.operation, "receipt": error.receipt},
             error={"code": error.code, "retry_allowed": False}, secret=secret)
        return 1
    except (EmojiError, MessageError) as error:
        emit(False, args.command, error={"code": error.code, "message": str(error), "retry_allowed": False}, secret=secret)
        return 1
    except ReadLimit as error:
        emit(False, args.command, coverage={"stop_reason": error.reason, "history_complete": False},
             error={"code": error.reason, "retry_allowed": False}, secret=secret)
        return 1
    except (XhhError, OSError, ValueError, TypeError, sqlite3.Error) as error:
        emit(False, args.command, error={"code": type(error).__name__, "message": str(error)}, secret=secret)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
