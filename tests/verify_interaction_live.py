from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import xhh
from operations import verify_identity
from xhh_sdk.accounts import AccountStore
from xhh_sdk.client import XhhClient

READ_ROUTES = {"/account/restore_login", "/bbs/app/profile/bbs/comment/list",
               "/bbs/app/user/message", "/bbs/app/link/tree", "/bbs/app/comment/sub/comments"}
CONTENT_COLUMNS = {"text", "raw_text", "body", "media_json", "payload_json", "raw_json",
                   "submitted_request", "request_context", "image_url"}


def guarded_client(config):
    client = XhhClient(config)
    original = client.transport.signed_request
    original_open = client.transport._open

    def request(route, *args, **kwargs):
        if route not in READ_ROUTES or kwargs.get("payload") is not None:
            raise RuntimeError("live acceptance rejected a non-read request")
        return original(route, *args, **kwargs)

    def open_read(request, *args, **kwargs):
        if request.get_method() != "GET" or request.data is not None or urlsplit(request.full_url).path not in READ_ROUTES:
            raise RuntimeError("live acceptance rejected a non-read request")
        return original_open(request, *args, **kwargs)

    client.transport.signed_request = request
    client.transport._open = open_read
    return client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-dir", type=Path, required=True)
    parser.add_argument("--account", default="xhh-test-web")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="live-interaction-metadata-", dir=output.parent)).resolve()
    assert run.is_relative_to(output.parent)
    data_dir = run / "history"
    summary = {"ok": False, "platform_writes": 0, "local_history": str(data_dir), "checks": {}}
    names = ("XHH_PKEY", "XHH_HEYBOX_ID", "XHH_PROTOCOL_MODE")
    previous = {name: os.environ.get(name) for name in names}

    def invoke(*arguments, local=False):
        captured = io.StringIO()
        factory = guarded_client
        if local:
            def factory(config):
                raise AssertionError("local history constructed a network client")
        with contextlib.redirect_stdout(captured):
            code = xhh.main(["--env", "--protocol", "web", "--data-dir", str(data_dir), *arguments],
                            client_factory=factory)
        value = json.loads(captured.getvalue())
        if code or not value.get("ok"):
            error = value.get("error") or {}
            summary["error"] = {key: error.get(key) for key in ("code", "status")}
            raise RuntimeError("live read stopped")
        return value

    try:
        config = AccountStore(args.account_dir).get_config(args.account)
        if config.protocol_mode != "web":
            raise RuntimeError("live acceptance requires the selected Web test account")
        client = guarded_client(config)
        verify_identity(client)
        summary["checks"]["identity_verified"] = True
        summary["account"] = {"alias": args.account, "uid_masked": "***" + str(config.heybox_id)[-2:]}
        raw = client.transport.signed_request("/bbs/app/profile/bbs/comment/list",
            query={"userid": str(config.heybox_id), "offset": "0", "limit": "2", "only_cy": "0"})
        rows = raw.get("result")
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("no own-comment sample available")
        chosen = next(row for row in rows if isinstance(row, dict) and
                      isinstance(row.get("link"), dict) and row["link"].get("id"))
        post = str(chosen["link"]["id"])
        cid = str(chosen.get("comment_id") or chosen.get("commentid"))
        for name, value in zip(names, (config.pkey, str(config.heybox_id), "web")):
            os.environ[name] = value

        imported = invoke("my-comments", "--limit", "10")
        summary["checks"]["own_comment_live_body_present"] = any(
            isinstance(row, dict) and isinstance(row.get("text"), str) for row in imported["data"])
        assert summary["checks"]["own_comment_live_body_present"]
        state = invoke("interaction", "status", post, local=True)["data"]
        assert state["participation"] == "confirmed" and state["my_known_comment_count"] >= 1
        assert state["history_complete"] is False
        summary["checks"]["known_participation"] = state["my_known_comment_count"]
        history = invoke("interaction", "history", post, "--limit", "20", local=True)["data"]
        own = next(item for item in history["items"] if item["id"] == cid)
        assert own["author"]["uid"] == str(config.heybox_id)
        assert not CONTENT_COLUMNS.intersection(own)
        assert "media" not in own
        summary["checks"]["offline_metadata_history"] = True

        post_result = invoke("post", post, "--requests", "1")
        summary["checks"]["single_request_post"] = post_result["coverage"]["budget"]["used_requests"] == 1
        assert summary["checks"]["single_request_post"]
        summary["checks"]["post_body_status"] = post_result["data"]["body"]["status"]
        inbox = invoke("inbox", "--limit", "2", "--requests", "2")
        entries = inbox["data"].get("entries", inbox["data"].get("messages"))
        assert isinstance(entries, list)
        summary["checks"]["inbox_read"] = {"entries": len(entries),
            "identity_verified_this_read": inbox["coverage"]["identity_verified_this_read"]}
        assert summary["checks"]["inbox_read"]["identity_verified_this_read"]

        path = data_dir / "operations.sqlite3"
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as database:
            assert database.execute("PRAGMA user_version").fetchone()[0] == 2
            tables = ("interaction_comments", "interaction_write_context", "interaction_inbox", "interaction_sync_state")
            for table in tables:
                columns = {row[1] for row in database.execute("PRAGMA table_info(" + table + ")")}
                assert not CONTENT_COLUMNS.intersection(columns), (table, sorted(columns))
            serialized = "\n".join(database.iterdump())
            body = chosen.get("text")
            if isinstance(body, str) and len(body) >= 24:
                assert body not in serialized
            summary["checks"]["schema_version"] = 2
            summary["checks"]["content_columns_absent"] = True
            summary["checks"]["stored_comment_facts"] = database.execute("SELECT COUNT(*) FROM interaction_comments").fetchone()[0]
        summary["ok"] = True
    except Exception as error:
        summary.setdefault("error", {"type": type(error).__name__, "code": getattr(error, "code", None),
                                      "status": getattr(error, "status", None)})
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
