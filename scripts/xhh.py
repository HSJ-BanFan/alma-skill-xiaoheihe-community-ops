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
from xhh_sdk.exceptions import XhhError

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
    for name in ("feed", "inbox", "search", "drafts", "my-posts", "my-comments"):
        command = commands.add_parser(name)
        command.add_argument("--offset", type=nonnegative, default=0)
        command.add_argument("--limit", type=positive, default=20)
        if name == "feed":
            command.add_argument("topic_id", type=numeric_id)
        if name == "search":
            command.add_argument("query")
            command.add_argument("--type", choices=("link", "game", "user"), default="link")
    for name in ("post", "comments"):
        command = commands.add_parser(name)
        command.add_argument("link_id", type=numeric_id)
        command.add_argument("--page", type=positive, default=1)
        command.add_argument("--limit", type=positive, default=50)
    command = commands.add_parser("replies")
    command.add_argument("root_id", type=numeric_id)
    command.add_argument("--cursor")
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
            command.add_argument("--page", type=positive, default=1)
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
    print(json.dumps(redact({"version": 1, "ok": ok, "kind": kind, "data": data,
                     "coverage": coverage or {}, "error": error}, secret), ensure_ascii=False))


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
        config = load_config(args)
        secret = config.pkey
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
        else:
            data, coverage = read(client, args.command, args)
        emit(True, args.command, data, coverage=coverage, secret=secret)
        return 0
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
