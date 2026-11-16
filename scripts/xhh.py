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
from paging import delivery_ok, project_coverage
import browser
from corpus_cli import dispatch as dispatch_corpus
from corpus_cli import register as register_corpus

WRITES = {"comment", "reply", "like", "favourite", "unfavourite", "delete-comment", "delete-post", "upload", "publish"}


def public_reader_config(uid="0"):
    return XhhConfig(
        pkey="public-reader",
        heybox_id=uid or "0",
        protocol_mode="web",
        extra={"public_reader": True},
    )


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
    register_corpus(commands)
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
    reader = browser.READERS.get(getattr(args, "reader", None))
    if args.command == "post" and reader and reader.protocol == "public":
        if args.protocol:
            raise XhhConfigError(
                "the html reader is public and cannot use --protocol")
        identity = (
            AccountStore(args.data_dir).get(args.account).identity
            if args.account else "0"
        )
        return public_reader_config(identity)
    public_auto = (
        args.command == "post"
        and args.reader == "auto"
        and args.protocol is None
    )
    if public_auto and not (args.account or args.config or args.env):
        return public_reader_config()
    if args.account:
        try:
            config = AccountStore(args.data_dir).get_config(
                args.account, protocol=args.protocol)
        except XhhConfigError as error:
            if public_auto and error.code == "session_missing":
                account = AccountStore(args.data_dir).get(args.account)
                return public_reader_config(account.identity)
            raise
    elif args.config:
        config = XhhConfig.from_file(args.config)
        if args.protocol and config.protocol_mode != args.protocol:
            raise XhhConfigError(
                "explicit protocol does not match the legacy credential")
    elif args.env:
        config = XhhConfig.from_env()
        if args.protocol and config.protocol_mode != args.protocol:
            raise XhhConfigError(
                "explicit protocol does not match the environment credential")
    else:
        raise OperationError("account_selection_required")
    config.validate()
    return config


def load_clients(args, config, client_factory):
    if args.command != "post":
        return {config.protocol_mode: client_factory(config)}
    reader = browser.READERS.get(args.reader)
    if reader and reader.protocol == "public":
        if args.protocol:
            raise XhhConfigError(
                "the html reader is public and cannot use --protocol")
        return {config.protocol_mode: client_factory(config)}

    provider = reader.protocol if reader else None
    if provider and args.protocol and args.protocol != provider:
        raise XhhConfigError(
            f"{args.reader} reader requires the {provider} protocol",
            code=f"{provider}_session_missing",
        )
    if args.account:
        store = AccountStore(args.data_dir)
        account = store.get(args.account)
        if provider:
            protocols = (provider,)
        elif args.protocol:
            protocols = (args.protocol,)
        else:
            protocols = tuple(
                protocol for protocol in ("app", "web")
                if protocol in account.sessions
            )
        if not protocols:
            if (config.extra or {}).get("public_reader"):
                return {
                    config.protocol_mode: client_factory(config),
                }
            raise XhhConfigError(
                "auto reader has no stored App or Web session",
                code="session_missing",
            )
        return {
            protocol: client_factory(store.get_config(args.account, protocol=protocol))
            for protocol in protocols
        }
    if provider and config.protocol_mode != provider:
        raise XhhConfigError(
            f"{args.reader} reader requires the {provider} protocol",
            code=f"{provider}_session_missing",
        )
    return {config.protocol_mode: client_factory(config)}


def public_error(error, protocol=None):
    if isinstance(error, XhhError):
        value = error.public()
    else:
        value = {
            "code": "internal_error",
            "message": str(error),
            "type": type(error).__name__,
            "retry_allowed": False,
        }
    if value.get("code") in {"session_expired", "auth_expired"} and protocol:
        value["code"] = f"{protocol}_session_expired"
        value["fix"] = f"log in again with the {protocol} protocol"
    return value


def coverage_error(scope, protocol):
    scope = project_coverage(scope)
    if delivery_ok(scope):
        return None
    reason = scope.get("stop_reason")
    if reason == "platform_error" and isinstance(scope.get("error"), dict):
        value = dict(scope["error"])
        if value.get("code") in {"session_expired", "auth_expired"} and protocol:
            value["code"] = f"{protocol}_session_expired"
            value["fix"] = f"log in again with the {protocol} protocol"
        return value
    error = scope.get("error")
    if isinstance(error, dict):
        return error
    return {
        "code": reason or "read_stopped",
        "message": "read stopped before platform completion",
        "retry_allowed": scope.get("status") == "retryable_failure",
    }


def project_delivery(coverage, error=None):
    value = dict(coverage) if isinstance(coverage, dict) else {}
    if isinstance(error, dict):
        value["error"] = dict(error)
    projected = project_coverage(value)
    if delivery_ok(projected):
        return projected, None, True
    projected_error = coverage_error(projected, None)
    stopped = (
        projected_error
        if isinstance(projected.get("error"), dict)
        else error or projected_error
    )
    return projected, stopped, False


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
    version = 2 if kind in browser.COMMANDS or kind in {
        "asset", "snapshot", "corpus",
    } else 1
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
        if args.command == "corpus":
            return dispatch_corpus(
                args, client_factory=client_factory, parser=parser,
                emitter=emit)
        if args.command in WRITES and not args.confirm:
            raise OperationError("confirmation_required")
        if args.command == "asset":
            from assets import fetch
            data = fetch(args.url, args.data_dir, refresh=args.refresh, seconds=args.seconds)
            emit(True, args.command, data, coverage={"preview_scope": "first_frame"})
            return 0
        if (args.command == "snapshot"
                and args.action == "resume"
                and not (args.account or args.config or args.env)):
            from observations import Store
            store = Store(args.data_dir)
            uid, pending_snapshot = store.pending_by_name(args.name)
            pending = pending_snapshot["pending_response"]
            projected, pending_error, ok = project_delivery(
                pending.get("coverage") or {},
                pending.get("error"),
            )
            emit(
                ok,
                args.command,
                pending["data"],
                coverage={**projected, "replayed_delivery": True},
                error=pending_error,
            )
            acknowledge_delivery(
                store, uid, args.name, pending_snapshot["revision"])
            return 0 if ok else 1
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
                projected, pending_error, ok = project_delivery(
                    pending.get("coverage") or {},
                    pending.get("error"),
                )
                emit(ok, args.command, pending["data"],
                     coverage={**projected, "replayed_delivery": True},
                     error=pending_error, secret=secret)
                acknowledge_delivery(store, config.heybox_id, args.name, saved["revision"])
                return 0 if ok else 1
            token = saved["coverage"].get("resume")
            if not token:
                raise XhhConfigError("snapshot has no resumable continuation")
            resumed = parser().parse_args([*saved["request"], "--resume", token, "--snapshot", args.name])
            resumed_clients = load_clients(resumed, config, client_factory)
            resumed_client = next(iter(resumed_clients.values()))
            data, coverage = browser.Browser(
                resumed_client, clients=resumed_clients).execute(
                    resumed, verify_account=True, prior_data=saved["data"])
            delta = difference(saved["data"], data)
            combined = merge(saved["data"], data)
            merge_resumed_coverage(saved, coverage, delta)
            if isinstance(data.get("items"), list):
                data["items"] = [item for item in data["items"] if record_key(item) not in delta["unchanged"]]
            coverage, stopped, ok = project_delivery(coverage)
            pending = {"ok": ok, "data": redact(data, secret),
                       "coverage": coverage, "error": stopped}
            try:
                revision = store.save(config.heybox_id, args.name, saved["query_key"], redact(combined, secret), coverage,
                                      browser.request_arguments(resumed), expected_revision=saved["revision"], pending_response=pending)
            except SnapshotConflict as error:
                emit(False, args.command, data, coverage=coverage, error={"code": "snapshot_conflict", "current_revision": error.revision}, secret=secret)
                return 1
            emit(ok, args.command, data, coverage=coverage,
                 error=pending["error"], secret=secret)
            acknowledge_delivery(store, config.heybox_id, args.name, revision)
            return 0 if ok else 1
        if args.command == "operation":
            value = Journal(args.data_dir).read(config.heybox_id, args.operation_id)
            emit(value is not None, args.command, value, error=None if value else {"code": "operation_not_found"}, secret=secret)
            return 0 if value else 1
        clients = load_clients(args, config, client_factory)
        client = next(iter(clients.values()))
        config = client.config
        secret = config.pkey
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
                projected, pending_error, ok = project_delivery(
                    pending.get("coverage") or {},
                    pending.get("error"),
                )
                emit(ok, args.command, pending["data"],
                     coverage={**projected, "replayed_delivery": True},
                     error=pending_error, secret=secret)
                acknowledge_delivery(store, config.heybox_id, args.snapshot, existing["revision"])
                return 0 if ok else 1
            if args.resume and existing is not None and existing["query_key"] != key:
                raise XhhConfigError("resumed snapshot belongs to a different query")
            data, coverage = browser.Browser(client, clients=clients).execute(
                args, verify_account=bool(args.snapshot or args.since),
                prior_data=existing["data"] if args.resume and existing else None)
            if data is None:
                coverage, stopped, ok = project_delivery(coverage)
                emit(ok, args.command, None, coverage=coverage,
                     error=stopped, secret=secret)
                return 0 if ok else 1
            if previous is not None:
                coverage["delta"] = difference(previous["data"], data)
            if args.snapshot:
                combined = merge(existing["data"], data) if args.resume and existing else data
                if args.resume and existing:
                    merge_resumed_coverage(existing, coverage, difference(existing["data"], data))
                coverage, stopped, ok = project_delivery(coverage)
                pending = {"ok": ok, "data": redact(data, secret),
                           "coverage": coverage, "error": stopped}
                try:
                    revision = store.save(config.heybox_id, args.snapshot, key, redact(combined, secret), coverage,
                            browser.request_arguments(args), expected_revision=existing["revision"] if existing else 0, pending_response=pending)
                except SnapshotConflict as error:
                    emit(False, args.command, data, coverage=coverage, error={"code": "snapshot_conflict", "current_revision": error.revision}, secret=secret)
                    return 1
            coverage, stopped, ok = project_delivery(coverage)
            if not ok:
                emit(False, args.command, data, coverage=coverage,
                     error=stopped, secret=secret)
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
        emit(False, args.command,
             data=getattr(error, "partial_data", None),
             error=public_error(error, locals().get("config") and config.protocol_mode),
             secret=secret)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
