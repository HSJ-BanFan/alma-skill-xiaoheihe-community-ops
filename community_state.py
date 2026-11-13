"""Account-sharded community ledger; the only writer of its SQLite state."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict
import copy
import re

SCHEMA = """
create table if not exists posts(
 post_id text primary key,title text not null default '',body text not null default '',
 author_uid text not null default '',author_name text not null default '',owned integer not null default 0,
 feed_at real,detail_at real,delivered_at real,snapshot text not null default '{}');
create table if not exists nodes(
 cid text primary key,post_id text not null,root_id text not null default '',author_uid text not null default '',
 author_name text not null default '',reply_id text not null default '',reply_uid text not null default '',
 text text not null default '',created_at text not null default '',images text not null default '[]',
 observed_at real not null,source text not null);
create index if not exists node_post on nodes(post_id,root_id);
create index if not exists node_reply on nodes(reply_id,author_uid);
create table if not exists roots(post_id text not null,root_id text not null default '',kind text not null,
 last_attempt real not null default 0,last_success real not null default 0,primary key(post_id,root_id));
create table if not exists incoming(
 cid text primary key,post_id text not null,root_id text not null,author_uid text not null,author_name text not null,
 text text not null,created_at text not null,kind text not null,own_target text not null default '',
 first_discovered real not null,last_observed real not null);
create table if not exists dispositions(cid text primary key,kind text not null,decided_at real not null,actor text not null);
create table if not exists intents(intent text primary key,operation_id text not null);
create table if not exists attempts(
 operation_id text primary key,intent text not null,kind text not null,post_id text not null,target_cid text not null,
 root_id text not null,payload text not null,digest text not null,state text not null,cid text,
 reserved_at real not null,finished_at real,detail text not null default '{}',actor text not null);
create index if not exists attempts_intent on attempts(intent,reserved_at);
create table if not exists receipts(
 cid text primary key,operation_id text,post_id text not null,target_cid text not null default '',root_id text not null default '',
 author_uid text not null,text text not null default '',image_url text not null default '',captured_at real not null,
 source text not null,target_proof text not null);
create index if not exists receipt_target on receipts(target_cid,author_uid);
create table if not exists visibility(
 id integer primary key autoincrement,cid text not null,post_id text not null,kind text not null,
 observed_at real not null,scope text not null,source text not null);
create index if not exists visibility_cid on visibility(cid,observed_at desc,id desc);
create unique index if not exists visibility_evidence on visibility(cid,post_id,kind,observed_at,scope,source);
create table if not exists observations(
 id integer primary key autoincrement,kind text not null,object_id text not null,observed_at real not null,
 source text not null,scope text not null,unique(kind,object_id,observed_at,source,scope));
create table if not exists scans(
 scan_id text primary key,area text not null,state text not null,revision integer not null default 0,
 owner text not null,lease_until real not null,status text not null,created_at real not null,updated_at real not null);
create table if not exists meta(key text primary key,value text not null);
create table if not exists public_views(
 view_id text primary key,kind text not null,payload text not null,pages text not null,keys text not null,
 captured_at real not null,post_id text not null default '');
create table if not exists run_budgets(
 run_id text primary key,started_at real not null,deadline real not null,request_limit integer not null,
 write_limit integer not null,minimum_write_interval real not null,account_alias text not null,
 requests_used integer not null default 0);
create table if not exists run_grants(
 operation_id text primary key,run_id text not null,granted_at real not null,released_at real);
create index if not exists run_grant_usage on run_grants(run_id,released_at,granted_at);
create table if not exists reaction_acks(
 operation_id text primary key,kind text not null,post_id text not null,author_uid text not null,
 captured_at real not null,source text not null,account_proof text not null,target_proof text not null,facts text not null);
create index if not exists reaction_ack_target on reaction_acks(kind,post_id,author_uid);
create table if not exists reaction_history_unbound(
 observation_id text primary key,kind text not null,post_id text not null,source_key text not null,
 captured_at real not null,resolved_uid text not null default '',unique(kind,post_id,source_key));
"""

IMAGE_SCHEMA = """
create table if not exists image_assets(
 asset_id text primary key,view_id text not null,kind text not null,post_id text not null,
 run_id text not null,source_ref text not null,captured_at real not null,payload text not null);
create index if not exists image_asset_issuance on image_assets(run_id,post_id,view_id);
"""


class PreparedImage(TypedDict):
    snapshot_file: str
    source_sha256: str
    source_bytes: int
    source_mime_type: str
    source_animated: bool
    preview_file: str
    preview_sha256: str
    preview_bytes: int
    preview_width: int
    preview_height: int
    preview_scope: str


@dataclass(frozen=True)
class ImageIssue:
    source_ref: str
    prepared: PreparedImage


@dataclass(frozen=True)
class ImagePlan:
    required: tuple[dict, ...]
    selected: dict | None
    view_ref: str


REACTIONS = frozenset({"like", "favourite"})


def valid_reaction_ack(ack, kind: str, post_id: str, uid: str) -> bool:
    if (not isinstance(ack, dict) or set(ack) != {"kind", "post_id", "account_uid", "source", "account_proof", "target_proof", "facts"}
            or ack.get("kind") != kind or ack.get("post_id") != post_id or ack.get("account_uid") != uid
            or ack.get("target_proof") != "request"):
        return False
    if (ack.get("source"), ack.get("account_proof")) not in (
            ("original_helper_return", "configured_binding"), ("historical_helper_stdout", "event_binding")):
        return False
    facts = ack.get("facts")
    if not isinstance(facts, dict):
        return False
    if kind == "like":
        flag = facts.get("is_max_charge")
        return set(facts) == {"is_max_charge"} and (flag is None or (type(flag) is int and flag in (0, 1)))
    return (kind == "favourite" and set(facts) == {"favour_type", "default_folder", "api_status"}
            and facts.get("favour_type") == "1" and facts.get("default_folder") is True
            and facts.get("api_status") in ("ok", "success"))


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def payload_pages(text: str, limit: int = 5000) -> list[str]:
    """Split a JSON document into pages that are each valid JSON.

    Slicing the encoded text every N bytes produced pages that no caller could
    parse, so a paginated read could never be reassembled or acted on. When the
    document carries a list we split that list instead, keeping every page a
    complete object. Paths are returned so the caller can merge them back.
    """
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    split = _split_json_list(parsed, limit) if isinstance(parsed, dict) else None
    if split:
        return split
    pages = []
    while text:
        low, high = 1, min(len(text), limit)
        while low < high:
            middle = (low + high + 1) // 2
            if len(dumps(text[:middle]).encode("utf-8")) <= limit:
                low = middle
            else:
                high = middle - 1
        pages.append(text[:low])
        text = text[low:]
    return pages or [""]


def _split_json_list(node, limit: int, path: tuple = ()) -> list[str] | None:
    """Split the first oversized list found at any depth, keeping pages valid JSON."""
    if isinstance(node, dict):
        for key, value in node.items():
            found = _split_json_list(value, limit, path + (key,))
            if found:
                return found
        return None
    if not isinstance(node, list) or len(node) < 2:
        return None
    if len(dumps(node).encode("utf-8")) <= limit:
        return None
    return _paginate_list(node, limit, path)


def _paginate_list(items: list, limit: int, path: tuple) -> list[str] | None:
    pages, current = [], []
    for item in items:
        if current and len(dumps(current + [item]).encode("utf-8")) > limit:
            pages.append(current)
            current = [item]
        else:
            current = current + [item]
    if current:
        pages.append(current)
    if len(pages) < 2:
        return None
    return [dumps({"path": list(path), "items": page}) for page in pages]


class StateConflict(RuntimeError):
    pass


class RunLimit(StateConflict):
    def __init__(self, code: str, retry_after: float | None = None):
        self.code, self.retry_after = code, retry_after
        super().__init__(code)


class State:
    def __init__(self, uid: str, root: Path | None = None, *, image_contract: bool = False):
        self.image_contract = image_contract
        if not str(uid).isdigit():
            raise ValueError("invalid_account_identity")
        self.uid = str(uid)
        root = root or Path.home() / ".config/alma/state/community"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"{self.uid}.db"
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            if image_contract:
                conn.executescript(IMAGE_SCHEMA)
            conn.execute("insert or ignore into meta values('schema_version','1')")
            conn.commit()

    @contextmanager
    def connection(self, write: bool = False):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("pragma journal_mode=wal")
            conn.execute("pragma synchronous=full")
            conn.execute("pragma busy_timeout=5000")
            if write:
                conn.execute("begin immediate")
            yield conn
            if write:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _observe(self, conn, kind: str, key: str, now: float, source: str, scope: str):
        conn.execute("insert or ignore into observations(kind,object_id,observed_at,source,scope) values(?,?,?,?,?)",
                     (kind, key, now, source, scope))

    def _post(self, conn, post: dict, now: float, source: str, scope: str, kind: str):
        pid = post["post_id"]
        conn.execute("""insert into posts(post_id,title,body,author_uid,author_name,owned)
          values(?,?,?,?,?,?) on conflict(post_id) do update set
          title=case when excluded.title='' then posts.title else excluded.title end,
          author_uid=case when excluded.author_uid='' then posts.author_uid else excluded.author_uid end,
          author_name=case when excluded.author_name='' then posts.author_name else excluded.author_name end,
          owned=max(posts.owned,excluded.owned)""",
                     (pid, post.get("title", ""), post.get("body", ""), post.get("author_uid", ""),
                      post.get("author_name", ""), int(kind == "own_post" or post.get("author_uid") == self.uid)))
        if kind == "detail":
            conn.execute("update posts set body=?,detail_at=?,snapshot=? where post_id=?",
                         (post.get("body", ""), now, dumps(post), pid))
        elif kind == "feed":
            conn.execute("update posts set feed_at=? where post_id=?", (now, pid))
        if kind == "own_post" or post.get("author_uid") == self.uid:
            conn.execute("insert or ignore into roots(post_id,root_id,kind) values(?,'','own_post')", (pid,))
        self._observe(conn, kind, pid, now, source, scope)

    def _node(self, conn, node: dict, now: float, source: str, scope: str):
        cid, pid = node["cid"], node["post_id"]
        conn.execute("""insert into nodes(cid,post_id,root_id,author_uid,author_name,reply_id,reply_uid,text,created_at,images,observed_at,source)
          values(?,?,?,?,?,?,?,?,?,?,?,?) on conflict(cid) do update set
          root_id=case when excluded.root_id='' then nodes.root_id else excluded.root_id end,
          author_uid=case when excluded.author_uid='' then nodes.author_uid else excluded.author_uid end,
          author_name=case when excluded.author_name='' then nodes.author_name else excluded.author_name end,
          reply_id=case when excluded.reply_id='' then nodes.reply_id else excluded.reply_id end,
          reply_uid=case when excluded.reply_uid='' then nodes.reply_uid else excluded.reply_uid end,
          text=case when excluded.text='' then nodes.text else excluded.text end,
          created_at=case when excluded.created_at='' then nodes.created_at else excluded.created_at end,
          images=excluded.images,observed_at=max(nodes.observed_at,excluded.observed_at),source=excluded.source""",
                     (cid, pid, node.get("root_id", ""), node.get("author_uid", ""), node.get("author_name", ""),
                      node.get("reply_id", ""), node.get("reply_uid", ""), node.get("text", ""),
                      str(node.get("created_at") or ""), dumps(node.get("images") or []), now, source))
        conn.execute("insert or ignore into posts(post_id) values(?)", (pid,))
        if node.get("author_uid") == self.uid:
            if node.get("reply_id"):
                conn.execute("update receipts set target_cid=?,target_proof='observed' where cid=? and author_uid=? and target_proof='unknown'",
                             (node["reply_id"], cid, self.uid))
            root = node.get("root_id") or ""
            conn.execute("insert or ignore into roots(post_id,root_id,kind) values(?,?,'own_comment')", (pid, root))
        self._observe(conn, "node", cid, now, source, scope)
        if source not in {"own_profile", "receipt"}:
            conn.execute("insert or ignore into visibility(cid,post_id,kind,observed_at,scope,source) values(?,?,'exact_present',?,?,?)",
                         (cid, pid, now, scope, source))

    def _discover(self, conn, post_ids: set[str]):
        for pid in post_ids:
            owned = conn.execute("select owned from posts where post_id=?", (pid,)).fetchone()
            own = {row[0] for row in conn.execute("select cid from nodes where post_id=? and author_uid=?", (pid, self.uid))}
            own.update(row[0] for row in conn.execute("select cid from receipts where post_id=? and author_uid=?", (pid, self.uid)))
            for row in conn.execute("select * from nodes where post_id=? and author_uid<>? and author_uid<>''", (pid, self.uid)).fetchall():
                if not row["root_id"]:
                    continue
                target = row["reply_id"]
                if target in own:
                    kind = "reply_to_own_comment"
                elif owned and owned[0]:
                    kind, target = "comment_on_own_post", ""
                else:
                    continue
                conn.execute("""insert into incoming values(?,?,?,?,?,?,?,?,?,?,?) on conflict(cid) do update set
                  root_id=excluded.root_id,author_name=excluded.author_name,text=excluded.text,
                  last_observed=max(incoming.last_observed,excluded.last_observed)""",
                             (row["cid"], pid, row["root_id"], row["author_uid"], row["author_name"], row["text"],
                              row["created_at"], kind, target, row["observed_at"], row["observed_at"]))

    def ingest(self, records: list[dict], *, now: float | None = None, source: str = "live", scope: str = ""):
        captured = time.time() if now is None else now
        with self.connection(True) as conn:
            self._ingest(conn, records, captured, source, scope)

    def _ingest(self, conn, records: list[dict], now: float, source: str, scope: str):
        posts = set()
        for record in records:
            data = record["data"]
            posts.add(data["post_id"])
            if record["type"] == "node":
                self._node(conn, data, now, source, scope)
            else:
                self._post(conn, data, now, source, scope, record["type"])
        self._discover(conn, posts)

    def invalidate_delivery(self, post_id: str):
        with self.connection(True) as conn:
            conn.execute("update posts set delivered_at=null where post_id=?", (post_id,))
            conn.execute("delete from meta where key=?", ("delivery:" + post_id,))

    def delivered(self, post_id: str, observed_cids: set[str], scopes: list[str]):
        now = time.time()
        with self.connection(True) as conn:
            conn.execute("update posts set delivered_at=? where post_id=?", (now, post_id))
            conn.execute("insert into meta values(?,?) on conflict(key) do update set value=excluded.value",
                         ("delivery:" + post_id, dumps({"captured_at": now, "cids": sorted(observed_cids), "scopes": scopes})))

    def delivery(self, post_id: str) -> dict:
        with self.connection() as conn:
            row = conn.execute("select value from meta where key=?", ("delivery:" + post_id,)).fetchone()
            return json.loads(row[0]) if row else {"cids": [], "scopes": []}

    def post(self, post_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("select * from posts where post_id=?", (post_id,)).fetchone()
            if not row:
                return None
            out = dict(row)
            out["snapshot"] = json.loads(out["snapshot"])
            return out

    def node(self, cid: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("select * from nodes where cid=?", (cid,)).fetchone()
            return self._node_view(row) if row else None

    @staticmethod
    def _node_view(row) -> dict:
        out = dict(row)
        out["images"] = json.loads(out["images"])
        return out

    def floor(self, post_id: str, root_id: str) -> list[dict]:
        with self.connection() as conn:
            return [self._node_view(row) for row in conn.execute(
                "select * from nodes where post_id=? and root_id=? order by created_at,cid", (post_id, root_id))]

    def incoming(self, cid: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("select * from incoming where cid=?", (cid,)).fetchone()
            return self._incoming_view(conn, row) if row else None

    def _incoming_view(self, conn, row, now: float | None = None) -> dict:
        out = dict(row)
        out["ref"] = "i:" + out["cid"]
        decision = conn.execute("select * from dispositions where cid=?", (out["cid"],)).fetchone()
        out["disposition"] = dict(decision) if decision else None
        out["defer_until"] = decision["decided_at"] + 24 * 3600 if decision and decision["kind"] == "defer" else None
        out["deferred"] = out["defer_until"] is not None and (time.time() if now is None else now) < out["defer_until"]
        exact = conn.execute("select cid from nodes where post_id=? and author_uid=? and reply_id=? order by created_at,cid",
                             (out["post_id"], self.uid, out["cid"])).fetchall()
        receipts = conn.execute("select cid from receipts where post_id=? and author_uid=? and target_cid=? and target_proof<>'unknown' order by captured_at,cid",
                                (out["post_id"], self.uid, out["cid"])).fetchall()
        exact_ids, receipt_ids = {item[0] for item in exact}, {item[0] for item in receipts}
        out["answered_observed"] = bool(exact)
        out["answer_observation_is_historical"] = True
        out["answer_cid"] = exact[0][0] if exact else None
        out["receipt_cid"] = receipts[0][0] if receipts else None
        out["reply_evidence"] = [{"ref": "c:" + cid, "cid": cid, "api_receipt": cid in receipt_ids,
                                  "exact_target_observed": cid in exact_ids, "visibility": self._visibility(conn, cid)}
                                 for cid in sorted(exact_ids | receipt_ids)]
        out["reply_count_known"] = len(out["reply_evidence"])
        out["handled"] = bool(exact or receipts or (decision and decision["kind"] == "close"))
        intent = f"reply:{out['post_id']}:{out['cid']}"
        op = conn.execute("select a.* from attempts a join intents i on a.operation_id=i.operation_id where i.intent=?", (intent,)).fetchone()
        out["operation"] = self._operation_view(conn, op) if op else None
        out["blocked_uncertain"] = bool(op and op["state"] in {"reserved", "uncertain"})
        return out

    def inbox(self, include_handled: bool = False, limit: int = 30) -> dict:
        with self.connection() as conn:
            rows = conn.execute("select * from incoming order by first_discovered desc,cid desc").fetchall()
            now = time.time()
            all_views = [self._incoming_view(conn, row, now) for row in rows]
            eligible = [row for row in all_views if
                        not row["handled"] and not row["blocked_uncertain"] and not row["deferred"]]
            items = (all_views if include_handled else eligible)[:limit]
            return {"items": items, "eligible": len(eligible), "displayed": len(items),
                    "observed_total": len(all_views), "handled_total": sum(row["handled"] for row in all_views),
                    "blocked_uncertain": sum(row["blocked_uncertain"] for row in all_views),
                    "queries_consume_newness": False}

    def dispose(self, cid: str, kind: str) -> dict:
        if kind not in {"close", "defer"}:
            raise ValueError("invalid_disposition")
        with self.connection(True) as conn:
            if not conn.execute("select 1 from incoming where cid=?", (cid,)).fetchone():
                raise ValueError("incoming_not_discovered")
            conn.execute("insert into dispositions values(?,?,?,?) on conflict(cid) do update set kind=excluded.kind,decided_at=excluded.decided_at,actor=excluded.actor",
                         (cid, kind, time.time(), "local_tool"))
        return self.incoming(cid) or {}

    def begin_run(self, scope) -> dict:
        if scope.account_uid != self.uid:
            raise StateConflict("run_account_mismatch")
        now = time.time()
        if (scope.contract_version != 2 or not isinstance(scope.run_id, str) or not 0 < len(scope.run_id) <= 200 or
                scope.account_alias not in {"alt", "main"} or type(scope.request_limit) is not int or not 1 <= scope.request_limit <= 120 or
                type(scope.write_limit) is not int or not 1 <= scope.write_limit <= 3 or
                any(type(value) not in {int, float} or not math.isfinite(value)
                    for value in (scope.started_at, scope.deadline, scope.minimum_write_interval)) or
                not 0 < scope.started_at <= now + 5 or scope.deadline <= scope.started_at or scope.minimum_write_interval < 15):
            raise StateConflict("run_scope_invalid")
        if now >= scope.deadline:
            raise RunLimit("run_deadline")
        fields = ("run_id", "started_at", "deadline", "request_limit", "write_limit", "minimum_write_interval", "account_alias")
        values = tuple(getattr(scope, field) for field in fields)
        with self.connection(True) as conn:
            row = conn.execute("select * from run_budgets where run_id=?", (scope.run_id,)).fetchone()
            if row and tuple(row[field] for field in fields) != values:
                raise StateConflict("run_scope_changed")
            if not row:
                conn.execute("insert into run_budgets(run_id,started_at,deadline,request_limit,write_limit,minimum_write_interval,account_alias) values(?,?,?,?,?,?,?)", values)
            return self._run_usage(conn, scope.run_id)

    @staticmethod
    def _run_row(conn, run_id: str):
        row = conn.execute("select * from run_budgets where run_id=?", (run_id,)).fetchone()
        if not row:
            raise RunLimit("run_not_registered")
        stopped = conn.execute("select value from meta where key=?", ("run-stop:" + run_id,)).fetchone()
        if stopped:
            raise RunLimit("run_stopped")
        now = time.time()
        if not math.isfinite(now) or now < row["started_at"] - 5:
            raise RunLimit("run_clock_not_verified")
        if now >= row["deadline"]:
            raise RunLimit("run_deadline")
        return row

    def _consume_run_request(self, conn, run_id: str):
        row = self._run_row(conn, run_id)
        if row["requests_used"] >= row["request_limit"]:
            raise RunLimit("run_request_budget")
        conn.execute("update run_budgets set requests_used=requests_used+1 where run_id=?", (run_id,))

    def consume_run_request(self, run_id: str):
        with self.connection(True) as conn:
            self._consume_run_request(conn, run_id)

    def _grant_run_write(self, conn, run_id: str, operation_id: str, now: float):
        row = self._run_row(conn, run_id)
        used, latest = conn.execute("select count(*),max(granted_at) from run_grants where run_id=? and released_at is null", (run_id,)).fetchone()
        if used >= row["write_limit"]:
            raise RunLimit("run_write_budget")
        if latest is not None and now < latest + row["minimum_write_interval"]:
            raise RunLimit("run_write_interval", latest + row["minimum_write_interval"] - now)
        # Grant the send operation here too: quota refusal occurs before any send reservation commits.
        self._consume_run_request(conn, run_id)
        conn.execute("insert into run_grants(operation_id,run_id,granted_at) values(?,?,?)", (operation_id, run_id, now))

    def _run_usage(self, conn, run_id: str) -> dict:
        row = conn.execute("select * from run_budgets where run_id=?", (run_id,)).fetchone()
        if not row:
            raise RunLimit("run_not_registered")
        used, latest = conn.execute("select count(*),max(granted_at) from run_grants where run_id=? and released_at is null", (run_id,)).fetchone()
        return {"run_id": run_id, "account_uid": self.uid, "account_alias": row["account_alias"],
                "started_at": row["started_at"], "deadline": row["deadline"],
                "requests_used": row["requests_used"], "requests_limit": row["request_limit"],
                "request_unit": "backend_operation_grants", "writes_used": used, "writes_limit": row["write_limit"],
                "minimum_write_interval": row["minimum_write_interval"],
                "next_write_at": latest + row["minimum_write_interval"] if latest is not None else None,
                "queries_consume_budget": False}

    def run_usage(self, run_id: str) -> dict:
        with self.connection() as conn:
            usage = self._run_usage(conn, run_id)
            stopped = conn.execute("select value from meta where key=?", ("run-stop:" + run_id,)).fetchone()
            if stopped:
                usage["stop_reason"] = json.loads(stopped[0])
            return usage

    @staticmethod
    def _stop_run(conn, run_id: str, reason: dict):
        safe = {key: reason[key] for key in ("code", "type", "operation_id") if isinstance(reason.get(key), str)}
        conn.execute("insert or ignore into meta values(?,?)", ("run-stop:" + run_id, dumps(safe)))

    def stop_run(self, run_id: str, reason: dict):
        with self.connection(True) as conn:
            if conn.execute("select 1 from run_budgets where run_id=?", (run_id,)).fetchone():
                self._stop_run(conn, run_id, reason)

    def publish_view(self, result: dict, *, delivery: dict | None = None,
                     images: tuple[ImageIssue, ...] = (), run_id: str | None = None) -> dict:
        rows = []
        if images:
            if not self.image_contract or result["kind"] not in {"detail", "stickers"}:
                raise StateConflict("image_contract_not_active")
            result = copy.deepcopy(result)
            items = result["data"]["post"]["assets"] if result["kind"] == "detail" else result["data"]
            field = "url" if result["kind"] == "detail" else "file"
            sources = {item.get(field) for item in items}
            for issue in images:
                prepared = issue.prepared
                if (issue.source_ref not in sources or
                        any(not isinstance(prepared.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", prepared[key])
                            for key in ("source_sha256", "preview_sha256")) or
                        any(type(prepared.get(key)) is not int or prepared[key] <= 0
                            for key in ("source_bytes", "preview_bytes", "preview_width", "preview_height")) or
                        prepared["source_bytes"] > 10 * 1024 * 1024 or prepared["preview_bytes"] > 320 * 1024 or
                        max(prepared["preview_width"], prepared["preview_height"]) > 1600 or
                        prepared.get("preview_scope") != "first_frame" or type(prepared.get("source_animated")) is not bool or
                        any(not isinstance(prepared.get(key), str) or not Path(prepared[key]).is_absolute()
                            for key in ("snapshot_file", "preview_file"))):
                    raise StateConflict("invalid_prepared_image")
                asset = dict(prepared, asset_id=uuid.uuid4().hex)
                rows.append((issue.source_ref, asset))
                for item in items:
                    if item.get(field) == issue.source_ref:
                        item.update(asset, file=asset["snapshot_file"], image_status="prepared_not_observed")
                        if field == "url":
                            item["local_file"] = asset["snapshot_file"]
        text = dumps(result)
        size = len(text.encode("utf-8"))
        # Each detail issuance has its own confirmation scope, even if the body is unchanged.
        identity = text + (uuid.uuid4().hex if delivery else "")
        view_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        pieces = [text] if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 6000 else payload_pages(text)
        page_keys = [uuid.uuid4().hex for _ in pieces]
        now = time.time()
        with self.connection(True) as conn:
            conn.execute("delete from public_views where captured_at<?", (now - 7 * 86400,))
            conn.execute("insert or ignore into public_views values(?,?,?,?,?,?,?)",
                         (view_id, result["kind"], text, dumps(pieces), dumps(page_keys),
                          now, delivery["post_id"] if delivery else ""))
            for source_ref, asset in rows:
                conn.execute("insert into image_assets values(?,?,?,?,?,?,?,?)",
                             (asset["asset_id"], view_id, result["kind"], delivery["post_id"] if delivery else "",
                              run_id or "", source_ref, now, dumps(asset)))
            if delivery:
                pid = delivery["post_id"]
                metadata = {"captured_at": now, "cids": sorted(delivery["cids"]), "scopes": delivery["scopes"],
                            "view_ref": "v:" + view_id, "delivery_state": "issued_not_host_confirmed"}
                if self.image_contract:
                    post_assets = (result.get("data") or {}).get("post", {}).get("assets", [])
                    metadata.update(required_image_refs=[asset["url"] for asset in post_assets],
                                    image_asset_ids=[asset["asset_id"] for _, asset in rows], image_run_id=run_id or "")
                conn.execute("update posts set delivered_at=? where post_id=?", (now, pid))
                conn.execute("insert into meta values(?,?) on conflict(key) do update set value=excluded.value",
                             ("delivery:" + pid, dumps(metadata)))
        if len(pieces) == 1:
            return self.view_page(view_id, 1)
        original = result.get("data")
        data = {"view_ref": "v:" + view_id, "original_kind": result["kind"], "payload_bytes": size}
        if isinstance(original, dict):
            for key in ("operation_id", "returned_cid", "state", "ref"):
                if isinstance(original.get(key), str) and len(original[key]) <= 200:
                    data[key] = original[key]
            for key in ("eligible", "displayed", "observed_total", "handled_total", "blocked_uncertain"):
                if key in original:
                    data[key] = original[key]
            post = original.get("post")
            if isinstance(post, dict):
                data["post"] = {"post_id": post.get("post_id"), "ref": post.get("ref"), "title": str(post.get("title", ""))[:200]}
            posts = original.get("posts")
            if isinstance(posts, list):
                data["posts_preview"] = [{"post_id": post.get("post_id"), "ref": post.get("ref"),
                                          "title": str(post.get("title", ""))[:160]} for post in posts[:8]]
                data["posts_in_snapshot"] = len(posts)
        previous = result.get("coverage") or {}
        coverage = {key: previous[key] for key in ("status", "stop", "resume", "usage", "run_budget") if key in previous}
        coverage.update({"payload_state": "partial", "payload_pages": len(pieces), "full_history": False,
                         "full_coverage_in_view": True, "gaps_in_snapshot": len(previous.get("gaps", []))})
        header = {"version": 1, "ok": result["ok"], "kind": result["kind"], "account": result.get("account"),
                  "data": data, "coverage": coverage, "errors": result.get("errors", [])[:3],
                  # Every page key up front: callers otherwise re-read pages or scrape
                  # keys with a regex, which is where the round-trip cost and errors came from.
                  "context": {"view_ref": "v:" + view_id, "pages": len(pieces),
                              "keys": page_keys, "status": "issued_not_confirmed"}}
        if len(json.dumps(header, ensure_ascii=False).encode("utf-8")) > 8000:
            # The complete fields remain in the immutable view, including oversized error scopes.
            header["data"] = {"view_ref": "v:" + view_id, "original_kind": result["kind"], "payload_bytes": size}
            header["coverage"] = {"payload_state": "partial", "payload_pages": len(pieces),
                                  "full_history": False, "full_coverage_in_view": True}
            header["errors"] = [{"code": str(error.get("code", "result_error"))[:80]} for error in result.get("errors", [])[:3]]
        return header

    @staticmethod
    def _view_page(row, source: dict, pieces: list[str], keys: list[str], page: int) -> dict:
        if page < 1 or page > len(pieces):
            raise StateConflict("view_page_out_of_range")
        if len(pieces) == 1:
            result = source
        else:
            result = {"version": 1, "ok": True, "kind": "view", "account": source.get("account"),
                      "data": {"format": "json_fragment", "text": pieces[page - 1], "original_kind": row["kind"]},
                      "coverage": {"payload_state": "page", "page": page, "payload_pages": len(pieces),
                                   "view_snapshot": True, "full_history": False}, "errors": []}
        result["context"] = {"view_ref": "v:" + row["view_id"], "page": page, "pages": len(pieces),
                             "key": keys[page - 1], "status": "issued_not_host_confirmed"}
        return result

    def view_page(self, view_id: str, page: int = 1) -> dict:
        with self.connection() as conn:
            row = conn.execute("select * from public_views where view_id=? and captured_at>=?",
                               (view_id, time.time() - 7 * 86400)).fetchone()
            if not row:
                raise StateConflict("view_not_recorded")
            return self._view_page(row, json.loads(row["payload"]), json.loads(row["pages"]), json.loads(row["keys"]), page)

    @staticmethod
    def _context_row(conn, post_id: str, keys: tuple[str, ...]):
        saved = conn.execute("select value from meta where key=?", ("delivery:" + post_id,)).fetchone()
        delivery = json.loads(saved[0]) if saved else {}
        ref = delivery.get("view_ref", "")
        row = conn.execute("select * from public_views where view_id=? and post_id=?", (ref[2:], post_id)).fetchone()
        if not row or not 0 <= time.time() - row["captured_at"] <= 900:
            raise StateConflict("context_confirmation_required")
        expected = json.loads(row["keys"])
        if len(keys) != len(expected) or set(keys) != set(expected):
            raise StateConflict("context_confirmation_required")
        return row, delivery

    def context_pages(self, post_id: str, keys: tuple[str, ...]) -> list[dict]:
        with self.connection() as conn:
            row, _ = self._context_row(conn, post_id, keys)
            pieces, expected, source = json.loads(row["pages"]), json.loads(row["keys"]), json.loads(row["payload"])
            return [self._view_page(row, source, pieces, expected, page) for page in range(1, len(pieces) + 1)]

    def _image_plan(self, conn, post_id: str, keys: tuple[str, ...], run_id: str, *,
                    image_file: str | None = None, image_url: str | None = None) -> ImagePlan:
        row, delivery = self._context_row(conn, post_id, keys)
        refs = delivery.get("required_image_refs", [])
        if not refs and image_file is None and image_url is None:
            return ImagePlan((), None, "v:" + row["view_id"])
        if not self.image_contract or not run_id or delivery.get("image_run_id") != run_id:
            raise StateConflict("image_run_not_bound")
        now = time.time()
        # Reuse any asset this run already prepared for this post. Re-reading a post
        # issues a new view_id, so binding to view_id alone would discard assets the
        # caller already saw and selected. Match on run + post instead.
        issued = conn.execute(
            "select * from image_assets where run_id=? and captured_at>=?"
            " and ((post_id=? and post_id<>'') or kind='stickers')",
            (run_id, now - 900, post_id)).fetchall()
        available = {item["asset_id"]: dict(json.loads(item["payload"]), source_ref=item["source_ref"],
                                            issued_at=item["captured_at"])
                     for item in issued if 0 <= now - item["captured_at"] <= 900}
        required = []
        # Match required sources against every asset this run prepared for the post,
        # not only the ids listed by the latest view. A re-read replaces that list, so
        # using it alone would drop images the caller already had confirmed.
        for source in dict.fromkeys(refs):
            matched = [asset for asset in available.values() if asset["source_ref"] == source]
            if not matched:
                raise StateConflict("required_image_unavailable")
            # The same post may be re-read several times in one run, which issues a
            # fresh asset for the same source each time. Take the newest one instead
            # of demanding a single match, otherwise a re-read bricks the send.
            required.append(max(matched, key=lambda asset: asset.get("issued_at", 0)))
        selected = None
        if image_file is not None or image_url is not None:
            if image_file is not None and image_url is not None:
                raise StateConflict("conflicting_images")
            expected = str(Path(image_file).expanduser().resolve()).casefold() if image_file is not None else None
            matched = [asset for asset in available.values() if
                       (expected is not None and str(Path(asset["snapshot_file"]).resolve()).casefold() == expected) or
                       (image_url is not None and asset["source_ref"] == image_url)]
            if not matched:
                raise StateConflict("image_selection_not_issued")
            selected = max(matched, key=lambda asset: asset.get("issued_at", 0))
            if selected["asset_id"] not in {asset["asset_id"] for asset in required}:
                required.append(selected)
        return ImagePlan(tuple(required), selected, "v:" + row["view_id"])

    def image_plan(self, post_id: str, keys: tuple[str, ...], run_id: str, *,
                   image_file: str | None = None, image_url: str | None = None) -> ImagePlan:
        with self.connection() as conn:
            return self._image_plan(conn, post_id, keys, run_id, image_file=image_file, image_url=image_url)

    @staticmethod
    def legacy_send_guard(uid: str, target: dict, text: str, *, root: Path,
                          operation_id: str | None = None):
        if not isinstance(uid, str) or not uid.isascii() or not uid.isdigit():
            raise StateConflict("legacy_account_unbound")
        path = root / f"{uid}.db"
        if not path.exists():
            if operation_id is not None:
                raise StateConflict("canonical_dispatch_not_bound")
            return
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("pragma query_only=1")
            row = conn.execute("select a.* from attempts a join intents i on a.operation_id=i.operation_id where i.intent=?",
                               (State.intent(target),)).fetchone()
            if operation_id is not None:
                if (row is None or row["operation_id"] != operation_id or row["state"] != "reserved" or
                        row["actor"] != f"local_tool:pid={os.getpid()}" or row["post_id"] != target["post_id"] or
                        row["target_cid"] != target.get("incoming_cid", "") or row["root_id"] != target.get("root_cid", "") or
                        json.loads(row["payload"]).get("text") != text):
                    raise StateConflict("canonical_dispatch_not_bound")
                return
            if row and row["state"] != "refused":
                raise StateConflict("canonical_intent_closed")
            if target["kind"] == "comment":
                known = conn.execute("select cid from receipts where post_id=? and author_uid=? and target_cid='' union all select cid from nodes where post_id=? and author_uid=? and (root_id=cid or root_id='') limit 1",
                                     (target["post_id"], uid, target["post_id"], uid)).fetchone()
            else:
                known = conn.execute("select cid from receipts where post_id=? and author_uid=? and (target_cid=? or (root_id=? and target_proof='unknown')) union all select cid from nodes where post_id=? and author_uid=? and reply_id=? limit 1",
                                     (target["post_id"], uid, target.get("incoming_cid", ""), target.get("root_cid", ""),
                                      target["post_id"], uid, target.get("incoming_cid", ""))).fetchone()
            if known:
                raise StateConflict("canonical_intent_closed")
        finally:
            conn.close()

    @staticmethod
    def intent(target: dict) -> str:
        if target["kind"] in REACTIONS:
            pid = target.get("post_id")
            if (not isinstance(pid, str) or not pid.isascii() or not pid.isdigit() or not 1 <= len(pid) <= 32 or int(pid) == 0
                    or str(int(pid)) != pid or target.get("incoming_cid") or target.get("root_cid")):
                raise StateConflict("invalid_reaction_target")
            return f"{target['kind']}:{pid}:"
        return f"{target['kind']}:{target['post_id']}:{target.get('incoming_cid', '')}"

    def reserve(self, target: dict, payload: dict, *, run_id: str | None = None,
                context_keys: tuple[str, ...] | None = None, image_plan: ImagePlan | None = None,
                image_proofs: tuple[dict, ...] | None = None) -> dict:
        key = self.intent(target)
        encoded = dumps(payload)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.connection(True) as conn:
            now = time.time()
            if context_keys is not None:
                _, delivery = self._context_row(conn, target["post_id"], context_keys)
                if target.get("incoming_cid") and target["incoming_cid"] not in delivery["cids"]:
                    raise StateConflict("target_not_currently_observed")
                if self.image_contract:
                    chosen = image_plan.selected["snapshot_file"] if image_plan and image_plan.selected else None
                    current = self._image_plan(conn, target["post_id"], context_keys, run_id or "", image_file=chosen)
                    if payload.get("image_url") is not None or (
                            payload.get("image_file") is not None and
                            (current.selected is None or str(Path(payload["image_file"]).resolve()).casefold() !=
                             str(Path(current.selected["snapshot_file"]).resolve()).casefold())):
                        raise StateConflict("image_confirmation_required")
                    if current.required or payload.get("image_file") or payload.get("image_url"):
                        if image_plan != current:
                            raise StateConflict("image_confirmation_required")
                        proofs = image_proofs or ()
                        expected = {asset["asset_id"]: asset for asset in current.required}
                        if len(proofs) != len(expected) or {proof.get("asset_id") for proof in proofs} != set(expected):
                            raise StateConflict("image_confirmation_required")
                        for proof in proofs:
                            asset = expected[proof["asset_id"]]
                            if (proof.get("status") != "host_preview_bytes_verified" or
                                    proof.get("preview_sha256") != asset["preview_sha256"] or proof.get("preview_bytes") != asset["preview_bytes"] or
                                    proof.get("provider_bytes_observed") is not False or
                                    proof.get("byte_scope") not in {"host_saved_inline_bytes", "current_gallery_cache_bytes"} or
                                    type(proof.get("checked_at")) not in {int, float} or
                                    not math.isfinite(proof["checked_at"]) or not 0 <= now - proof["checked_at"] <= 900):
                                raise StateConflict("image_confirmation_required")
            elif self.image_contract and image_plan is not None:
                raise StateConflict("context_confirmation_required")
            previous = conn.execute("select a.* from attempts a join intents i on a.operation_id=i.operation_id where i.intent=?", (key,)).fetchone()
            if previous and previous["state"] != "refused":
                out = self._operation_view(conn, previous)
                out["fresh"] = False
                out["status"] = "conflict" if previous["digest"] != digest else "blocked" if previous["state"] in {"reserved", "uncertain"} else "acknowledged" if previous["state"] == "acknowledged" else "returned"
                return out
            if target["kind"] in REACTIONS:
                gap = conn.execute("""select g.id from observations g where g.kind='historical_reaction_gap' and g.object_id=?
                    and not exists(select 1 from observations r where r.kind='historical_reaction_gap_resolved'
                                   and r.object_id=g.object_id and r.source=g.source) limit 1""", (target["kind"],)).fetchone()
                if gap:
                    return {"fresh": False, "status": "historical_reaction_scope_unresolved", "evidence": gap[0]}
                unbound = conn.execute("select observation_id from reaction_history_unbound where kind=? and post_id=? and resolved_uid='' limit 1",
                                       (target["kind"], target["post_id"])).fetchone()
                if unbound:
                    return {"fresh": False, "status": "historical_reaction_account_unresolved", "evidence": unbound[0]}
            # Prior observed/receipted comments are also one canonical automated intent.
            if target["kind"] == "comment":
                seen = conn.execute("select cid from nodes where post_id=? and author_uid=? and root_id=cid limit 1", (target["post_id"], self.uid)).fetchone()
                unresolved = conn.execute("select cid from nodes where post_id=? and author_uid=? and root_id='' and reply_id='' limit 1",
                                          (target["post_id"], self.uid)).fetchone()
                if unresolved:
                    return {"fresh": False, "status": "own_comment_scope_unresolved", "evidence": unresolved[0]}
                old = conn.execute("select cid from receipts where post_id=? and author_uid=? and target_cid='' and target_proof='request' limit 1", (target["post_id"], self.uid)).fetchone()
                if seen or old:
                    return {"fresh": False, "status": "already_commented", "returned_cid": (seen or old)[0]}
            elif target["kind"] == "reply":
                unbound = conn.execute("""select cid from receipts where post_id=? and root_id=? and author_uid=? and target_proof='unknown'
                    union all select operation_id from attempts where post_id=? and root_id=? and kind='reply' and target_cid='' and state in ('reserved','uncertain') limit 1""",
                    (target["post_id"], target.get("root_cid", ""), self.uid, target["post_id"], target.get("root_cid", ""))).fetchone()
                if unbound:
                    return {"fresh": False, "status": "unresolved_historical_target", "evidence": unbound[0]}
                incoming = conn.execute("select * from incoming where cid=? and post_id=?", (target["incoming_cid"], target["post_id"])).fetchone()
                if not incoming:
                    raise ValueError("incoming_not_discovered")
                view = self._incoming_view(conn, incoming)
                if view["handled"]:
                    return {"fresh": False, "status": "already_handled", "returned_cid": view["receipt_cid"] or view["answer_cid"]}
            op = uuid.uuid4().hex
            if run_id is not None:
                self._grant_run_write(conn, run_id, op, now)
            conn.execute("insert into attempts(operation_id,intent,kind,post_id,target_cid,root_id,payload,digest,state,reserved_at,actor) values(?,?,?,?,?,?,?,?,'reserved',?,?)",
                         (op, key, target["kind"], target["post_id"], target.get("incoming_cid", ""), target.get("root_cid", ""),
                          encoded, digest, now, f"local_tool:pid={os.getpid()}"))
            conn.execute("insert into intents values(?,?) on conflict(intent) do update set operation_id=excluded.operation_id", (key, op))
            row = conn.execute("select * from attempts where operation_id=?", (op,)).fetchone()
            out = self._operation_view(conn, row)
            out["fresh"] = True
            out["status"] = "reserved"
            return out

    def _put_reaction_ack(self, conn, operation: str, ack: dict, captured_at: float):
        conn.execute("insert or ignore into reaction_acks values(?,?,?,?,?,?,?,?,?)",
                     (operation, ack["kind"], ack["post_id"], self.uid, captured_at, ack["source"],
                      ack["account_proof"], ack["target_proof"], dumps(ack["facts"])))

    def _reaction_ack_view(self, conn, operation: str) -> dict | None:
        row = conn.execute("select * from reaction_acks where operation_id=? and author_uid=?", (operation, self.uid)).fetchone()
        if not row:
            return None
        return {"kind": row["kind"], "post_id": row["post_id"], "account_uid": row["author_uid"],
                "captured_at": row["captured_at"], "source": row["source"], "account_proof": row["account_proof"],
                "target_proof": row["target_proof"], "facts": json.loads(row["facts"]), "effect_verification": "not_observed"}

    def record_result(self, operation: str, receipt: dict | None = None, refusal: dict | None = None, error: dict | None = None) -> dict:
        with self.connection(True) as conn:
            row = conn.execute("select * from attempts where operation_id=?", (operation,)).fetchone()
            if not row:
                raise ValueError("unknown_operation")
            if row["state"] in {"returned", "acknowledged"}:
                return self._operation_view(conn, row)
            reaction = row["kind"] in REACTIONS
            ack = receipt.get("reaction_ack") if reaction and isinstance(receipt, dict) and set(receipt) == {"reaction_ack"} else None
            if not valid_reaction_ack(ack, row["kind"], row["post_id"], self.uid):
                ack = None
            value = receipt.get("comment_id") if not reaction and isinstance(receipt, dict) else None
            cid = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).isdigit() and int(value) > 0 else ""
            known = bool(cid or ack)
            if row["state"] in {"uncertain", "refused"} and not known:
                return self._operation_view(conn, row)
            grant = conn.execute("select run_id from run_grants where operation_id=?", (operation,)).fetchone()
            proved_refusal = refusal is not None and row["state"] == "reserved" and (
                refusal.get("dispatched") is False or (not reaction and not grant and "dispatched" not in refusal))
            state = "acknowledged" if ack else "returned" if cid else "refused" if proved_refusal else "uncertain"
            missing = "missing_reaction_ack" if reaction else "missing_returned_id"
            detail = (error or {}) if known else refusal or error or {"code": missing}
            if grant and (refusal or error or {}).get("stop") is True:
                self._stop_run(conn, grant["run_id"], dict(refusal or error or {}, operation_id=operation))
            now = time.time()
            conn.execute("update attempts set state=?,cid=?,finished_at=?,detail=? where operation_id=?",
                         (state, cid or None, now, dumps(detail), operation))
            if grant and proved_refusal and not known:
                conn.execute("update run_grants set released_at=? where operation_id=? and released_at is null", (now, operation))
            elif grant and known:
                conn.execute("update run_grants set released_at=null where operation_id=?", (operation,))
            if ack:
                self._put_reaction_ack(conn, operation, ack, now)
            if cid:
                payload = json.loads(row["payload"])
                conn.execute("insert or ignore into receipts values(?,?,?,?,?,?,?,?,?,?,?)",
                             (cid, operation, row["post_id"], row["target_cid"], row["root_id"], self.uid,
                              payload.get("text", ""), (receipt or {}).get("image_url") or "", now, "api_receipt", "request"))
                root = row["root_id"] or cid
                conn.execute("insert or ignore into roots(post_id,root_id,kind) values(?,?,'own_comment')", (row["post_id"], root))
                self._discover(conn, {row["post_id"]})
            return self._operation_view(conn, conn.execute("select * from attempts where operation_id=?", (operation,)).fetchone())

    @staticmethod
    def _visibility(conn, cid: str) -> dict:
        row = conn.execute("select * from visibility where cid=? order by observed_at desc,id desc limit 1", (cid,)).fetchone()
        out = dict(row) if row else {"kind": "not_observed"}
        if row:
            out["scope"] = json.loads(row["scope"]) if row["scope"].startswith(("[", "{")) else row["scope"]
        return out

    def comment(self, cid: str) -> dict | None:
        with self.connection() as conn:
            receipt = conn.execute("select * from receipts where cid=? and author_uid=?", (cid, self.uid)).fetchone()
            node = conn.execute("select * from nodes where cid=? and author_uid=?", (cid, self.uid)).fetchone()
            if not receipt and not node:
                return None
            source = receipt if receipt else node
            return {"ref": "c:" + cid, "cid": cid, "post_id": source["post_id"],
                    "root_id": source["root_id"] or cid, "receipt": dict(receipt) if receipt else None,
                    "observed_node": self._node_view(node) if node else None, "visibility": self._visibility(conn, cid)}

    def _operation_view(self, conn, row) -> dict:
        out = dict(row)
        out["payload"] = json.loads(out["payload"])
        out["detail"] = json.loads(out["detail"])
        out["returned_cid"] = out.pop("cid")
        out["ref"] = "op:" + out["operation_id"]
        if out["kind"] in REACTIONS:
            out["visibility"] = {"kind": "not_applicable"}
            out["acknowledgement"] = self._reaction_ack_view(conn, out["operation_id"])
        else:
            out["visibility"] = self._visibility(conn, out["returned_cid"])
        return out

    def operation(self, operation: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("select * from attempts where operation_id=?", (operation,)).fetchone()
            return self._operation_view(conn, row) if row else None

    def operation_for_target(self, target: dict) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("select a.* from attempts a join intents i on a.operation_id=i.operation_id where i.intent=?",
                               (self.intent(target),)).fetchone()
            return self._operation_view(conn, row) if row else None

    def observe_visibility(self, post_id: str, observed_cids: set[str], scope: dict, error: str | None = None,
                           *, captured_at: float | None = None, source: str = "read_back"):
        now = time.time() if captured_at is None else captured_at
        with self.connection(True) as conn:
            owned = conn.execute("select cid from receipts where post_id=? union select cid from nodes where post_id=? and author_uid=?",
                                 (post_id, post_id, self.uid)).fetchall()
            for row in owned:
                kind = "exact_present" if row["cid"] in observed_cids else "read_failed" if error else "absent_in_scope"
                conn.execute("insert or ignore into visibility(cid,post_id,kind,observed_at,scope,source) values(?,?,?,?,?,?)",
                             (row["cid"], post_id, kind, now, dumps(scope), source))

    def origin(self, lane: str) -> dict:
        with self.connection() as conn:
            row = conn.execute("select value from meta where key=?", ("origin:" + lane,)).fetchone()
            return json.loads(row[0]) if row else {"offset": 0, "anchors": []}

    def recent_activity(self, limit: int = 8, max_chars: int = 2000) -> dict:
        if (not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 30 or
                not isinstance(max_chars, int) or isinstance(max_chars, bool) or not 0 <= max_chars <= 12000):
            raise ValueError("invalid_activity_budget")
        with self.connection() as conn:
            conn.execute("begin")
            total = conn.execute("""select
                (select count(*) from receipts where author_uid=?) +
                (select count(*) from reaction_acks where author_uid=?) +
                (select count(*) from attempts where cid is null and state in ('reserved','uncertain'))""", (self.uid, self.uid)).fetchone()[0]
            rows = conn.execute("""select 'api_receipt' as publication_proof,'c:'||r.cid as ref,
                case when r.target_cid<>'' then 'reply' when r.target_proof='request' then 'comment'
                     else 'comment_or_reply_unknown' end as kind,
                r.post_id,coalesce(p.title,'') as post_title,r.target_cid,'returned' as state,
                r.captured_at,r.operation_id,r.cid as returned_cid,r.text,r.source as receipt_source,r.target_proof,
                'api_receipt' as source
                from receipts r left join posts p on p.post_id=r.post_id where r.author_uid=?
                union all select 'api_acknowledgement','op:'||r.operation_id,r.kind,r.post_id,coalesce(p.title,''),
                '','acknowledged',r.captured_at,r.operation_id,null,'',r.source,r.target_proof,'api_acknowledgement'
                from reaction_acks r left join posts p on p.post_id=r.post_id where r.author_uid=?
                union all select 'not_established','op:'||a.operation_id,a.kind,a.post_id,coalesce(p.title,''),
                a.target_cid,a.state,coalesce(a.finished_at,a.reserved_at),a.operation_id,null,'','','',
                case when a.actor='historical_tool_event' then 'historical_send_observation' else 'local_send_attempt' end
                from attempts a left join posts p on p.post_id=a.post_id
                where a.cid is null and a.state in ('reserved','uncertain')
                order by captured_at desc,ref desc limit ?""", (self.uid, self.uid, limit)).fetchall()
            items, remaining = [], max_chars
            for row in rows:
                item = {key: row[key] for key in ("ref", "kind", "state", "captured_at", "publication_proof", "source")}
                item["post_ref"] = "p:" + row["post_id"]
                title = row["post_title"]
                item["post_title"] = title[:remaining]
                remaining -= len(item["post_title"])
                if len(title) > len(item["post_title"]):
                    item["post_title_omitted_chars"] = len(title) - len(item["post_title"])
                if row["target_cid"]:
                    item["target_ref"] = "i:" + row["target_cid"]
                if row["operation_id"]:
                    item["operation_ref"] = "op:" + row["operation_id"]
                if row["publication_proof"] == "api_receipt":
                    body = row["text"]
                    item.update({"returned_cid": row["returned_cid"], "receipt_source": row["receipt_source"],
                                 "target_proof": row["target_proof"], "text_source": "receipt_request_text",
                                 "text": body[:remaining], "visibility": self._visibility(conn, row["returned_cid"])})
                    remaining -= len(item["text"])
                    if len(body) > len(item["text"]):
                        item["text_omitted_chars"] = len(body) - len(item["text"])
                elif row["publication_proof"] == "api_acknowledgement":
                    item["acknowledgement"] = self._reaction_ack_view(conn, row["operation_id"])
                items.append(item)
        return {"data_only": True, "source": "local_public_ledger", "captured_at": time.time(),
                "order": "ledger_capture_time_desc", "items": items, "recorded_total": total,
                "displayed": len(items), "text_chars": max_chars - remaining, "full_history": False}

    def recent_posts(self, limit: int = 2) -> list[str]:
        with self.connection() as conn:
            return [row[0] for row in conn.execute("""select post_id,max(captured) as newest from (
                select post_id,cast(created_at as real) as captured from nodes where author_uid=?
                union all select post_id,captured_at as captured from receipts where author_uid=?
                ) group by post_id order by newest desc limit ?""", (self.uid, self.uid, limit))]

    def retained_posts(self, limit: int = 100) -> list[str]:
        with self.connection() as conn:
            return [row[0] for row in conn.execute("select post_id from roots group by post_id order by min(last_attempt),post_id limit ?", (limit,))]

    def roots_for(self, post_id: str) -> list[str]:
        with self.connection() as conn:
            return [row[0] for row in conn.execute("select root_id from roots where post_id=? and root_id<>'' order by last_attempt,root_id", (post_id,))]

    def mark_attempted(self, post_id: str, success: bool):
        now = time.time()
        with self.connection(True) as conn:
            conn.execute("update roots set last_attempt=? where post_id=?", (now, post_id))
            if success:
                conn.execute("update roots set last_success=? where post_id=?", (now, post_id))

    def start_scan(self, area: str, initial: dict, *, resume: str | None = None, seconds: float = 40) -> dict:
        now, owner = time.time(), uuid.uuid4().hex
        with self.connection(True) as conn:
            row = conn.execute("select * from scans where scan_id=?", (resume,)).fetchone() if resume else conn.execute(
                "select * from scans where area=? and status='partial' and lease_until<=? order by updated_at desc limit 1", (area, now)).fetchone()
            if resume and (not row or row["area"] != area):
                raise StateConflict("resume_scope_mismatch")
            if row and not resume and not json.loads(row["state"]).get("pending"):
                previous = json.loads(row["state"])
                cursor = previous.get("cursor", {})
                remaining = cursor.get("queue", [])[cursor.get("index", 0):]
                initial["deferred_subs"] = [task for task in remaining if task.get("type") == "sub"]
                # Refresh heads, but put interrupted retained-root work ahead of fresh detail scans.
                row = None
            if row:
                if row["lease_until"] > now:
                    raise StateConflict("scan_already_running")
                state = json.loads(row["state"])
                scan_id, revision = row["scan_id"], row["revision"]
                conn.execute("update scans set owner=?,lease_until=?,status='running',updated_at=? where scan_id=?", (owner, now + seconds + 60, now, scan_id))
            else:
                scan_id, revision, state = uuid.uuid4().hex, 0, initial
                conn.execute("insert into scans values(?,?,?,0,?,?,'running',?,?)", (scan_id, area, dumps(state), owner, now + seconds + 60, now, now))
            state.update({"_scan": scan_id, "_revision": revision, "_owner": owner})
            return state

    def _checkpoint(self, conn, session: dict, status: str = "running"):
        stored = {key: value for key, value in session.items() if not key.startswith("_")}
        result = conn.execute("update scans set state=?,revision=revision+1,status=?,updated_at=?,lease_until=case when ?='running' then lease_until else 0 end where scan_id=? and revision=? and owner=?",
                              (dumps(stored), status, time.time(), status, session["_scan"], session["_revision"], session["_owner"]))
        if result.rowcount != 1:
            raise StateConflict("stale_scan_checkpoint")
        session["_revision"] += 1

    def checkpoint(self, session: dict, status: str = "running"):
        with self.connection(True) as conn:
            self._checkpoint(conn, session, status)

    def ingest_messages(self, messages: list[dict], *, now: float | None = None) -> int:
        """Import the platform reply queue into the same ledger the scanner fills.

        Scanning my own comments only finds replies under floors it already knows
        about, so a reply on an older post can stay invisible while the ledger
        reports itself clear. The platform queue names the sender, root and post
        directly, so importing it makes discovery independent of scan coverage.
        """
        captured = time.time() if now is None else now
        added = 0
        with self.connection(True) as conn:
            for item in messages:
                cid = str(item.get("comment_id") or "").strip()
                pid = str(item.get("link_id") or "").strip()
                if not cid or not pid or item.get("is_cy"):
                    continue
                sender = str(item.get("author_uid") or "").strip()
                if sender == self.uid:
                    continue
                root = str(item.get("root_id") or "").strip() or cid
                exists = conn.execute("select 1 from incoming where cid=?", (cid,)).fetchone()
                conn.execute("""insert into incoming values(?,?,?,?,?,?,?,?,?,?,?) on conflict(cid) do update set
                  root_id=excluded.root_id,author_name=excluded.author_name,text=excluded.text,
                  last_observed=max(incoming.last_observed,excluded.last_observed)""",
                             (cid, pid, root, sender, str(item.get("from") or ""), str(item.get("text") or ""),
                              str(item.get("created_at") or ""), "reply_to_own_comment",
                              str(item.get("my_comment_id") or ""), captured, captured))
                if not exists:
                    added += 1
                conn.execute("insert or ignore into posts(post_id) values(?)", (pid,))
                conn.execute("insert or ignore into roots(post_id,root_id,kind) values(?,?,'platform_message')", (pid, root))
        return added

    def consume_staged(self, session: dict, count: int) -> int:
        pending = session.get("pending")
        if not pending:
            return 0
        start = pending["position"]
        items = pending["rows"][start:start + count]
        with self.connection(True) as conn:
            self._ingest(conn, items, pending["captured_at"], pending["source"], pending["scope"])
            pending["position"] += len(items)
            if pending["position"] == len(pending["rows"]):
                next_cursor = dict(pending["next"])
                origin = next_cursor.pop("origin_update", None)
                if origin:
                    key = "origin:" + origin["lane"]
                    row = conn.execute("select value from meta where key=?", (key,)).fetchone()
                    prior = json.loads(row[0]) if row else {}
                    if prior.get("captured_at", 0) <= pending["captured_at"]:
                        saved = dict(origin, captured_at=pending["captured_at"])
                        conn.execute("insert into meta values(?,?) on conflict(key) do update set value=excluded.value", (key, dumps(saved)))
                    else:
                        session.setdefault("gaps", []).append({"code": "origin_cursor_concurrent", "scope": origin["lane"]})
                session["cursor"] = next_cursor
                session["pending"] = None
            self._checkpoint(conn, session)
        return len(items)

    def latest_coverage(self) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("select state,status,scan_id,updated_at from scans order by updated_at desc limit 1").fetchone()
            if not row:
                return None
            state = json.loads(row["state"])
            return {"scan_id": row["scan_id"], "status": row["status"], "updated_at": row["updated_at"],
                    "scopes": state.get("scopes", []), "gaps": state.get("gaps", []), "full_history": False}

    def import_receipt(self, receipt: dict):
        # Imported target provenance comes from parsed invocation, never timestamps/text.
        with self.connection(True) as conn:
            conn.execute("insert or ignore into posts(post_id) values(?)", (receipt["post_id"],))
            conn.execute("insert or ignore into receipts values(?,?,?,?,?,?,?,?,?,?,?)",
                         (receipt["cid"], None, receipt["post_id"], receipt.get("target_cid", ""), receipt.get("root_id", ""),
                          self.uid, receipt.get("text", ""), receipt.get("image_url", ""), receipt["captured_at"],
                          "historical_api_receipt", receipt.get("target_proof", "unknown")))
            root = receipt.get("root_id") or (receipt["cid"] if receipt.get("target_proof") == "request" and not receipt.get("target_cid") else "")
            conn.execute("insert or ignore into roots(post_id,root_id,kind) values(?,?,'historical_receipt')", (receipt["post_id"], root))
            self._discover(conn, {receipt["post_id"]})

    def observe_reaction_gap(self, kind: str, source_key: str, captured_at: float):
        if kind not in REACTIONS or not math.isfinite(captured_at) or captured_at < 0:
            raise StateConflict("invalid_historical_reaction")
        with self.connection(True) as conn:
            self._observe(conn, "historical_reaction_gap", kind, captured_at, source_key, "target_not_proved")

    def resolve_reaction_gap(self, kind: str, source_key: str, captured_at: float):
        if kind not in REACTIONS or not math.isfinite(captured_at) or captured_at < 0:
            raise StateConflict("invalid_historical_reaction")
        with self.connection(True) as conn:
            self._observe(conn, "historical_reaction_gap_resolved", kind, captured_at, source_key, "target_or_foreign_uid_proved")

    def observe_unbound_reaction(self, target: dict, source_key: str, captured_at: float):
        key = self.intent(target)
        if target["kind"] not in REACTIONS or not math.isfinite(captured_at) or captured_at < 0:
            raise StateConflict("invalid_historical_reaction")
        observation = hashlib.sha256((source_key + "|" + key).encode("utf-8")).hexdigest()[:32]
        with self.connection(True) as conn:
            conn.execute("insert or ignore into reaction_history_unbound(observation_id,kind,post_id,source_key,captured_at) values(?,?,?,?,?)",
                         (observation, target["kind"], target["post_id"], source_key, captured_at))

    def resolve_reaction_history(self, target: dict, source_key: str, account_uid: str):
        self.intent(target)
        if target["kind"] not in REACTIONS or not isinstance(account_uid, str) or not account_uid.isascii() or not account_uid.isdigit():
            raise StateConflict("invalid_historical_reaction_binding")
        with self.connection(True) as conn:
            conn.execute("update reaction_history_unbound set resolved_uid=? where kind=? and post_id=? and source_key=? and resolved_uid=''",
                         (account_uid, target["kind"], target["post_id"], source_key))

    def reaction_history(self, post_id: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("select observation_id,kind,captured_at from reaction_history_unbound where post_id=? and resolved_uid='' order by captured_at desc,observation_id limit 30",
                                (post_id,)).fetchall()
            return [{"observation_id": row["observation_id"], "kind": row["kind"], "post_ref": "p:" + post_id,
                     "captured_at": row["captured_at"], "source": "historical_parsed_target", "account_binding": "not_established"}
                    for row in rows]

    def import_reaction(self, target: dict, source_key: str, captured_at: float, acknowledgement: dict | None = None):
        key = self.intent(target)
        if (target["kind"] not in REACTIONS or not math.isfinite(captured_at) or captured_at < 0 or
                (acknowledgement is not None and (not valid_reaction_ack(acknowledgement, target["kind"], target["post_id"], self.uid)
                                                  or acknowledgement["account_proof"] != "event_binding"))):
            raise StateConflict("invalid_historical_reaction")
        operation = hashlib.sha256((source_key + "|" + key).encode("utf-8")).hexdigest()[:32]
        with self.connection(True) as conn:
            conn.execute("insert or ignore into posts(post_id) values(?)", (target["post_id"],))
            conn.execute("""insert or ignore into attempts(operation_id,intent,kind,post_id,target_cid,root_id,payload,digest,state,reserved_at,finished_at,detail,actor)
                values(?,?,?,?,'','','{}','','uncertain',?,?,?,'historical_tool_event')""",
                         (operation, key, target["kind"], target["post_id"], captured_at, captured_at,
                          dumps({"code": "historical_dispatch_unknown", "source_key": source_key})))
            if acknowledgement is not None:
                self._put_reaction_ack(conn, operation, acknowledgement, captured_at)
                conn.execute("update attempts set state='acknowledged',cid=null,detail='{}' where operation_id=?", (operation,))
            row = conn.execute("select state from attempts where operation_id=?", (operation,)).fetchone()
            prior = conn.execute("select a.state from intents i join attempts a on i.operation_id=a.operation_id where i.intent=?", (key,)).fetchone()
            if prior is None or (row["state"] == "acknowledged" and prior["state"] != "acknowledged"):
                conn.execute("insert into intents values(?,?) on conflict(intent) do update set operation_id=excluded.operation_id", (key, operation))
            conn.execute("update reaction_history_unbound set resolved_uid=? where kind=? and post_id=? and source_key=? and resolved_uid=''",
                         (self.uid, target["kind"], target["post_id"], source_key))

    def import_uncertain(self, target: dict, source_key: str, captured_at: float):
        if target["kind"] in REACTIONS:
            self.import_reaction(target, source_key, captured_at)
            return
        key = self.intent(target)
        operation = hashlib.sha256((source_key + "|" + key).encode("utf-8")).hexdigest()[:32]
        with self.connection(True) as conn:
            conn.execute("insert or ignore into posts(post_id) values(?)", (target["post_id"],))
            conn.execute("""insert or ignore into attempts(operation_id,intent,kind,post_id,target_cid,root_id,payload,digest,state,reserved_at,finished_at,detail,actor)
              values(?,?,?,?,?,?,'{}','','uncertain',?,?,?,'historical_tool_event')""",
                         (operation, key, target["kind"], target["post_id"], target.get("incoming_cid", ""), target.get("root_cid", ""),
                          captured_at, captured_at, dumps({"code": "historical_dispatch_unknown", "source_key": source_key})))
            prior = conn.execute("select a.reserved_at from intents i join attempts a on i.operation_id=a.operation_id where i.intent=?", (key,)).fetchone()
            if not prior or prior[0] <= captured_at:
                conn.execute("insert into intents values(?,?) on conflict(intent) do update set operation_id=excluded.operation_id", (key, operation))
            conn.execute("insert or ignore into roots(post_id,root_id,kind) values(?,?,'historical_uncertain')", (target["post_id"], target.get("root_cid", "")))

    def counts(self) -> dict:
        with self.connection() as conn:
            return {table: conn.execute(f"select count(*) from {table}").fetchone()[0]
                    for table in ("posts", "nodes", "incoming", "dispositions", "attempts", "receipts", "roots")}
