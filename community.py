"""Stable community CLI. Invalid requests are rejected before loading an SDK."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ACTIVE_CONTRACT = 2


class BoundaryError(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code)


def envelope(kind: str, data=None, *, account=None, errors=None, coverage=None) -> dict:
    problems = errors or []
    return {"version": 1, "ok": not problems, "kind": kind, "account": account,
            "data": data, "coverage": coverage, "errors": problems}


def bounded_summary(data: dict | None) -> dict | None:
    if not isinstance(data, dict):
        return None
    summary = {key: data[key] for key in ("ref", "operation_id", "state", "returned_cid", "cid", "post_id", "target_cid", "root_id", "receipt_persistence")
               if isinstance(data.get(key), str) and len(data[key]) <= 200}
    for key in ("eligible", "displayed", "observed_total", "handled_total", "blocked_uncertain"):
        value = data.get(key)
        if type(value) is int and 0 <= value <= 2 ** 63 - 1:
            summary[key] = value
    post = data.get("post")
    if isinstance(post, dict) and isinstance(post.get("post_id"), str) and re.fullmatch(r"[0-9]{1,32}", post["post_id"]):
        summary["post_ref"] = "p:" + post["post_id"]
    return summary or None


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise BoundaryError("invalid_arguments", message)


def parser() -> argparse.ArgumentParser:
    p = JSONArgumentParser(description="Account-scoped community observations and actions")
    p.add_argument("--account", choices=("alt", "main"), default="alt")
    sub = p.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="Refresh incoming observations or feed summaries")
    scan.add_argument("area", choices=("incoming", "feed"))
    scan.add_argument("--requests", type=int, default=12)
    scan.add_argument("--rows", type=int, default=300)
    scan.add_argument("--seconds", type=float, default=40)
    scan.add_argument("--resume", default=None)
    scan.add_argument("--topics", default="7214,549999,1,23563,615783,807953,475516,21325")
    read = sub.add_parser("read", help="Read a local view, exact target, or asset resource")
    read.add_argument("resource", help="account, inbox, knowledge, shortcodes, stickers, taste, p:ID, i:ID, c:ID, op:ID, v:HASH")
    read.add_argument("--page", type=int, default=1, help="Read one local recovery page from v:HASH")
    read.add_argument("--query", default="")
    read.add_argument("--all", action="store_true", dest="include_handled")
    read.add_argument("--limit", type=int, default=30)
    read.add_argument("--requests", type=int, default=3)
    read.add_argument("--seconds", type=float, default=40)
    read.add_argument("--refresh", action="store_true", help="Refresh a comment or operation's bounded visibility scope")
    act = sub.add_parser("act", help="Act on one resolved target or record an incoming disposition")
    act.add_argument("resource")
    act.add_argument("action", choices=("comment", "reply", "like", "favourite", "close", "defer"))
    act.add_argument("--text-file", type=Path)
    act.add_argument("--image-file", type=Path)
    act.add_argument("--image-url")
    act.add_argument("--without-image", metavar="REASON")
    act.add_argument("--context-key", action="append", default=[], help="Acknowledge one issued detail page")
    act.add_argument("--seconds", type=float, default=120, help="Complete action-worker deadline")
    act.add_argument("--confirm", action="store_true")
    return p


def valid_resource(value: str) -> bool:
    return value in {"account", "inbox", "knowledge", "shortcodes", "stickers", "taste"} or bool(
        re.fullmatch(r"(?:p|i|c):[0-9]+|op:[a-f0-9-]+|v:[a-f0-9]{32}", value))


def validate(args, *, read_files: bool = True) -> str:
    if args.command == "scan":
        if not 1 <= args.requests <= 100 or not 1 <= args.rows <= 2000 or not 0 < args.seconds <= 300:
            raise BoundaryError("invalid_budget")
        if args.area == "feed" and not all(re.fullmatch(r"[0-9]+", item) for item in args.topics.split(",")):
            raise BoundaryError("invalid_topics")
        return ""
    if not valid_resource(args.resource):
        raise BoundaryError("invalid_resource")
    if args.command == "read":
        if not 1 <= args.requests <= 30 or not 1 <= args.limit <= 300 or not 0 < args.seconds <= 300:
            raise BoundaryError("invalid_budget")
        if args.page < 1 or (args.page != 1 and not args.resource.startswith("v:")):
            raise BoundaryError("invalid_view_page")
        if args.resource.startswith("v:") and args.refresh:
            raise BoundaryError("view_refresh_unsupported")
        if (args.resource.startswith(("p:", "i:")) or args.refresh) and args.requests < 2:
            raise BoundaryError("invalid_budget")
        if args.resource in {"knowledge", "stickers"} and not args.query.strip():
            raise BoundaryError("query_required")
        return ""
    if not 0 < args.seconds <= 300:
        raise BoundaryError("invalid_budget")
    if args.context_key and (args.action in {"close", "defer"} or
            not all(re.fullmatch(r"[a-f0-9]{32}", key) for key in args.context_key)):
        raise BoundaryError("invalid_context_key")
    if args.action in {"reply", "comment", "like", "favourite"} and not args.confirm:
        raise BoundaryError("confirmation_required")
    if args.action == "reply" and (args.image_file is not None or args.image_url is not None):
        raise BoundaryError("reply_image_unsupported")
    expected = "p:" if args.action in {"comment", "like", "favourite"} else "i:"
    if not args.resource.startswith(expected):
        raise BoundaryError("invalid_action_target")
    if args.action in {"close", "defer"}:
        return ""
    if args.action in {"like", "favourite"}:
        if not re.fullmatch(r"p:[1-9][0-9]{0,31}", args.resource):
            raise BoundaryError("invalid_action_target")
        if any(value is not None for value in (args.text_file, args.image_file, args.image_url, args.without_image)):
            raise BoundaryError("reaction_payload_unsupported")
        return ""
    if args.text_file is None:
        raise BoundaryError("draft_required")
    if args.image_url is not None and not re.match(r"https?://[^\s]+$", args.image_url):
        raise BoundaryError("invalid_image_url")
    if args.image_file is not None and args.image_url is not None:
        raise BoundaryError("conflicting_images")
    if args.without_image is not None and not args.without_image.strip():
        raise BoundaryError("image_decision_reason_empty")
    if not read_files:
        return ""
    try:
        if args.text_file.stat().st_size > 4003:
            raise BoundaryError("platform_text_limit", "1000")
        text = args.text_file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise BoundaryError("draft_unreadable") from None
    if not text.strip():
        raise BoundaryError("draft_empty")
    if len(text) > 1000:
        raise BoundaryError("platform_text_limit", "1000")
    if args.image_file is not None:
        try:
            size = args.image_file.stat().st_size
        except OSError:
            raise BoundaryError("image_unreadable") from None
        if not args.image_file.is_file() or size > 10 * 1024 * 1024:
            raise BoundaryError("platform_image_limit")
    return text


def require_action_contract(args, contract_version: int) -> None:
    if args.command == "act" and args.action in {"like", "favourite"} and contract_version < 2:
        raise BoundaryError("reaction_not_active")


def execute(args, text: str, *, backend=None, state_root: Path | None = None, catalog=None,
            contract_version: int = 1, scope=None, evidence=None) -> dict:
    require_action_contract(args, contract_version)
    # This directory contains the user's installed tools, not downloaded samples.
    sys.path.insert(0, str(HERE))
    from community_bindings import WorkingBackend
    from community_facade import Action, Community, ReadBudget
    community = Community(backend if backend is not None else WorkingBackend(args.account), state_root, catalog,
                          contract_version=contract_version, run_scope=scope, evidence=evidence)
    if args.command == "scan":
        return community.scan(args.area, ReadBudget(args.requests, args.rows, args.seconds),
                              resume=args.resume, topics=tuple(args.topics.split(",")))
    if args.command == "read":
        return community.read(args.resource, ReadBudget(args.requests, 300, args.seconds),
                              query=args.query, include_handled=args.include_handled,
                              limit=args.limit, refresh=args.refresh, page=args.page)
    action = Action(args.resource, args.action, text,
                    str(args.image_file) if args.image_file is not None else None,
                    args.image_url, args.without_image, tuple(args.context_key))
    return community.act(action)


def print_result(value: dict):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)


def argument_result(kind: str, error: BoundaryError) -> dict:
    return envelope(kind, errors=[{"code": error.code, "detail": error.detail, "origin": "arguments"}])


def worker_main() -> int:
    sys.path.insert(0, str(HERE))
    from community_bindings import error_data
    from community_runtime import RuntimeProblem, validate_worker_request, worker_request, worker_result
    from community_state import StateConflict
    kind, payload, request_valid, binding = "worker", None, False, None
    def emit(value):
        nonlocal binding
        if value.get("kind") == "worker_binding":
            binding = value["data"]
        print_result(value)
    try:
        raw = sys.stdin.read(800001)
        if len(raw) > 800000:
            raise RuntimeProblem("worker_invalid_request")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise RuntimeProblem("worker_invalid_request") from None
        validate_worker_request(payload)
        request_valid = True
        result = worker_request(payload, emitter=emit)
    except BoundaryError as error:
        result, code = argument_result(kind, error), 2
    except (RuntimeProblem, HostProblem) as error:
        result, code = envelope(kind, errors=[{"code": error.code}]), 1
    except StateConflict as error:
        result, code = envelope(kind, errors=[error_data(error)]), 1
    except Exception as error:
        result, code = envelope(kind, errors=[{"code": "boundary_failure", "type": type(error).__name__}]), 1
    else:
        code = 0 if result.get("ok") else 1
    print_result(worker_result(payload, result, binding) if request_valid else result)
    return code


def main() -> int:
    if sys.argv[1:] == ["--worker"]:
        return worker_main()
    kind = "arguments"
    sys.path.insert(0, str(HERE))
    from community_runtime import RuntimeProblem, current_run_scope, supervise_cli
    from host_contract import HostProblem
    try:
        args = parser().parse_args()
        kind = args.command
        if ACTIVE_CONTRACT >= 2:
            validate(args, read_files=False)
            scope = current_run_scope()
            if scope is not None and scope.contract_version == 1:
                result = execute(args, validate(args))
            else:
                result = supervise_cli(sys.argv[1:], args.seconds, scope)
        else:
            result = execute(args, validate(args))
    except BoundaryError as error:
        result, code = argument_result(kind, error), 2
    except (RuntimeProblem, HostProblem) as error:
        result, code = envelope(kind, errors=[{"code": error.code}]), 1
    except Exception as error:
        # Keep tracebacks, credentials and private context out of routine tool output.
        result, code = envelope(kind, errors=[{"code": "boundary_failure", "type": type(error).__name__}]), 1
    else:
        code = 2 if any(error.get("origin") == "arguments" for error in result.get("errors", [])) else 0 if result.get("ok") else 1
    print_result(result)
    return code


if __name__ == "__main__":
    sys.modules["community"] = sys.modules[__name__]
    raise SystemExit(main())
