"""Recover legacy reply metadata through a bounded real-account root read."""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(Path(__file__).resolve().parent)]

import xhh
from verify_interaction_live import CONTENT_COLUMNS, guarded_client
from xhh_sdk.accounts import AccountStore


def masked(value):
    if isinstance(value, list):
        return [masked(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: "***" + str(item)[-2:] if key in {"uid", "account_uid", "peer_uid", "reply_to_uid"} and item is not None
            else masked(item) for key, item in value.items()}


def selected(state):
    keys = ("post_id", "participation", "my_known_comment_count", "completed_round_count", "count_semantics",
            "discussion_status", "relation_completeness", "unattributed_own_comment_count", "ancestry_complete",
            "own_reply_relations_complete", "history_complete", "peer_summaries", "missing_ancestor_ids", "unknown_parent_ids")
    return {key: state[key] for key in keys}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-dir", type=Path, required=True)
    parser.add_argument("--account", default="xhh-test-web")
    parser.add_argument("--seed-db", type=Path, required=True)
    parser.add_argument("--post-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="legacy-relations-live-", dir=output.parent)).resolve()
    assert run.is_relative_to(output.parent)
    state_dir = run / "history"
    state_dir.mkdir()
    summary = {"ok": False, "platform_writes": 0, "local_history": str(state_dir), "checks": {}}
    names = ("XHH_PKEY", "XHH_HEYBOX_ID", "XHH_PROTOCOL_MODE")
    previous = {name: os.environ.get(name) for name in names}
    calls = []

    def factory(config):
        client = guarded_client(config)
        request = client.transport.signed_request

        def record(route, *arguments, **kwargs):
            calls.append(route)
            return request(route, *arguments, **kwargs)

        client.transport.signed_request = record
        return client

    def offline(config):
        raise AssertionError("offline status constructed a network client")

    def invoke(*arguments, local=False):
        text = io.StringIO()
        with contextlib.redirect_stdout(text):
            code = xhh.main(["--env", "--protocol", config.protocol_mode, "--data-dir", str(state_dir), *arguments],
                            client_factory=offline if local else factory)
        result = json.loads(text.getvalue())
        if code or not result.get("ok"):
            summary["error"] = result.get("error")
            raise RuntimeError("legacy relation read stopped")
        return result

    try:
        config = AccountStore(args.account_dir).get_config(args.account)
        with contextlib.closing(sqlite3.connect(args.seed_db.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            owners = {row[0] for row in source.execute("SELECT DISTINCT uid FROM interaction_comments")}
            assert owners == {str(config.heybox_id)}
            root, = source.execute("""SELECT root_id FROM interaction_comments WHERE uid=? AND post_id=?
                AND own_list_seen_at IS NOT NULL AND reply_to_id IS NULL AND root_id IS NOT NULL AND comment_id!=root_id
                ORDER BY comment_id LIMIT 1""", (str(config.heybox_id), args.post_id)).fetchone()
            with contextlib.closing(sqlite3.connect(state_dir / "operations.sqlite3")) as target:
                source.backup(target)
        for name, value in zip(names, (config.pkey, str(config.heybox_id), config.protocol_mode)):
            os.environ[name] = value
        before = invoke("interaction", "status", args.post_id, local=True)["data"]
        assert before["participation"] == "confirmed"
        assert before["completed_round_count"] == 0
        assert before["count_semantics"] == "public_verified_lower_bound"
        assert before["relation_completeness"] == "partial"
        assert before["unattributed_own_comment_count"] > 0
        result = invoke("interaction", "sync", args.post_id, "--root", root, "--requests", "2", "--pages", "1", "--seconds", "30")
        after = result["data"]["status"]
        assert calls == ["/account/restore_login", "/bbs/app/comment/sub/comments"]
        assert result["coverage"]["budget"]["used_requests"] == 2
        assert after["completed_round_count"] > 0
        assert after["discussion_status"] == "confirmed"
        assert after["unattributed_own_comment_count"] == 0
        assert after["own_reply_relations_complete"] is True
        assert after["history_complete"] is False
        summary.update(before=selected(before), after=selected(after), root_id=root, routes=list(calls),
                       resume_available=bool(result["coverage"]["resume"]))
        peers = [peer for peer in after["peer_summaries"] if peer["completed_round_count"] > 0]
        assert peers
        peer_status = invoke("interaction", "status", args.post_id, "--peer", peers[0]["peer_uid"], local=True)["data"]
        assert peer_status["completed_round_count"] > 0
        repeated = invoke("interaction", "sync", args.post_id, "--root", root, "--requests", "2", "--pages", "1")["data"]["status"]
        assert repeated["completed_round_count"] == after["completed_round_count"]
        with contextlib.closing(sqlite3.connect((state_dir / "operations.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)) as stored:
            assert stored.execute("PRAGMA user_version").fetchone()[0] == 2
            for table in ("interaction_comments", "interaction_inbox", "interaction_sync_state", "interaction_write_context"):
                columns = {row[1] for row in stored.execute("PRAGMA table_info(" + table + ")")}
                assert not CONTENT_COLUMNS.intersection(columns)
            assert all(row[0] is not None for row in stored.execute("SELECT reply_to_id FROM interaction_comments WHERE uid=? AND post_id=? AND own_list_seen_at IS NOT NULL AND comment_id!=root_id",
                                                                 (str(config.heybox_id), args.post_id)))
        summary["checks"] = {"real_identity_verified": True, "known_root_prioritized": True,
                             "two_request_budget": True, "peer_query_recovered": True,
                             "repeat_did_not_inflate": True, "metadata_only_schema_v2": True}
        summary["ok"] = True
    except Exception as error:
        summary.setdefault("error", {"type": type(error).__name__, "code": getattr(error, "code", None), "status": getattr(error, "status", None)})
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        value = masked(summary)
        output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(value, ensure_ascii=False))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
