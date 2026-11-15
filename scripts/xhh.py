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
import browser

WRITES = {"comment", "reply", "like", "favourite", "unfavourite", "delete-comment", "delete-post", "upload", "publish"}


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
    for name in ("account-info", "emojis", "operation"):
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
    browser.register(commands)
    for name in sorted(WRITES):
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
    version = 2 if kind in browser.COMMANDS or kind in {"asset", "snapshot"} else 1
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
        if args.command == "asset":
            from assets import fetch
            data = fetch(args.url, args.data_dir, refresh=args.refresh, seconds=args.seconds)
            emit(True, args.command, data, coverage={"preview_scope": "first_frame"})
            return 0
        config = load_config(args)
        secret = config.pkey
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
            data, coverage = browser.Browser(client_factory(config)).execute(resumed, verify_account=True, prior_data=saved["data"])
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
            payload, send = prepare_write(client, args.command, args)
            data = Journal(args.data_dir).execute(config.heybox_id, args.operation_id, args.command, payload, send)
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
            data, coverage = browser.Browser(client).execute(args, verify_account=bool(args.snapshot or args.since), prior_data=existing["data"] if args.resume and existing else None)
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
        else:
            data, coverage = read(client, args.command, args)
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
    except (XhhError, OSError, ValueError, TypeError, sqlite3.Error) as error:
        emit(False, args.command, error={"code": type(error).__name__, "message": str(error)}, secret=secret)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
