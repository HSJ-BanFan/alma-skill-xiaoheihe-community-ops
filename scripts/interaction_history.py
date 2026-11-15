"""UID-scoped interaction facts in the canonical operation database."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from documents import identifier
from local_files import protect
from paging import decode_resume, encode_resume
from xhh_sdk.exceptions import XhhConfigError

SCHEMA_VERSION = 1
MIGRATION = (
    """CREATE TABLE IF NOT EXISTS operations (
        uid TEXT NOT NULL, operation_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        kind TEXT NOT NULL, status TEXT NOT NULL, receipt TEXT, updated_at REAL NOT NULL,
        PRIMARY KEY (uid, operation_id))""",
    """CREATE TABLE interaction_write_context (
        uid TEXT NOT NULL, operation_id TEXT NOT NULL, post_id TEXT NOT NULL,
        root_id TEXT, reply_to_id TEXT, payload_json TEXT NOT NULL, projected_at REAL,
        PRIMARY KEY (uid, operation_id),
        FOREIGN KEY (uid, operation_id) REFERENCES operations(uid, operation_id))""",
    "CREATE INDEX interaction_write_target ON interaction_write_context(uid,post_id,reply_to_id)",
    """CREATE TABLE interaction_comments (
        uid TEXT NOT NULL, comment_id TEXT NOT NULL, post_id TEXT NOT NULL,
        root_id TEXT, author_uid TEXT, reply_to_id TEXT, reply_to_uid TEXT,
        text TEXT, media_json TEXT, platform_created_at REAL,
        first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL,
        public_seen_at REAL, own_list_seen_at REAL, receipt_seen_at REAL,
        source_json TEXT NOT NULL, field_evidence_json TEXT NOT NULL,
        PRIMARY KEY (uid, comment_id))""",
    "CREATE INDEX interaction_post_author ON interaction_comments(uid,post_id,author_uid,platform_created_at,comment_id)",
    "CREATE INDEX interaction_thread_author ON interaction_comments(uid,post_id,root_id,author_uid)",
    "CREATE INDEX interaction_reply_target ON interaction_comments(uid,post_id,reply_to_id)",
    """CREATE TABLE interaction_inbox (
        uid TEXT NOT NULL, event_key TEXT NOT NULL, post_id TEXT NOT NULL,
        incoming_comment_id TEXT NOT NULL, root_id TEXT, peer_uid TEXT,
        first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL, raw_json TEXT NOT NULL,
        PRIMARY KEY (uid, event_key))""",
    "CREATE INDEX interaction_inbox_comment ON interaction_inbox(uid,post_id,incoming_comment_id)",
    """CREATE TABLE interaction_sync_state (
        uid TEXT NOT NULL, query_key TEXT NOT NULL, post_id TEXT, root_id TEXT, peer_uid TEXT,
        source TEXT NOT NULL, last_checked_at REAL NOT NULL, coverage_json TEXT NOT NULL,
        PRIMARY KEY (uid, query_key))""",
)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Scope:
    account_uid: str
    post_id: str
    peer_uid: str | None = None
    root_id: str | None = None

    def binding(self):
        return {"post_id": self.post_id, "peer_uid": self.peer_uid, "root_id": self.root_id}


@contextmanager
def database(path: Path, *, write=False):
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        protect(path.parent)
        connection = sqlite3.connect(path, timeout=10)
    else:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        if write:
            deadline = time.monotonic() + 10
            while True:
                try:
                    if connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                        connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as error:
                    if "locked" not in str(error) or time.monotonic() >= deadline:
                        raise
                    time.sleep(.02)
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise XhhConfigError("interaction database version is newer than this tool")
            if version == 0:
                for statement in MIGRATION:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
            protect(path)
            for suffix in ("-wal", "-shm"):
                companion = Path(str(path) + suffix)
                if companion.exists():
                    protect(companion)
        with connection:
            yield connection
    finally:
        connection.close()


def has_schema(connection):
    return connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def establish_identity(connection, uid, evidence):
    now = time.time()
    connection.execute("""INSERT INTO interaction_sync_state VALUES (?,?,NULL,NULL,NULL,?,?,?)
        ON CONFLICT(uid,query_key) DO UPDATE SET last_checked_at=excluded.last_checked_at,
        coverage_json=excluded.coverage_json""", (uid, "identity", "verified_identity", now,
        dumps({"account_uid": uid, "identity_established_at": now, "evidence": evidence})))


def bind_context(connection, uid, operation_id, kind, payload, *, identity_verified=False):
    if kind not in ("comment", "reply"):
        return
    post_id = identifier(payload.get("link_id"))
    root = identifier(payload.get("root_id"))
    target = identifier(payload.get("reply_id"))
    if not post_id or not isinstance(payload.get("text"), str) or kind == "reply" and (not root or not target):
        raise XhhConfigError("comment reservation requires complete post, text and reply context")
    connection.execute("""INSERT INTO interaction_write_context VALUES (?,?,?,?,?,?,NULL)
        ON CONFLICT(uid,operation_id) DO NOTHING""",
        (uid, operation_id, post_id, root, target, dumps(payload)))
    if identity_verified:
        establish_identity(connection, uid, {"source": "write_identity_check", "operation_id": operation_id})


SOURCE_RANK = {"receipt": 0, "inbox": 1, "own_list": 2, "public": 3}


def observe(connection, uid, node, source_kind, evidence):
    cid, post = identifier(node.get("id")), identifier(node.get("post_id"))
    if not cid or not post:
        raise XhhConfigError("interaction comment requires known CID and post membership")
    now = evidence.get("observed_at", time.time())
    row = connection.execute("SELECT * FROM interaction_comments WHERE uid=? AND comment_id=?", (uid, cid)).fetchone()
    if row is not None and row["post_id"] != post:
        raise XhhConfigError("comment CID has conflicting immutable post membership")
    fields = json.loads(row["field_evidence_json"]) if row else {}
    sources = json.loads(row["source_json"]) if row else {}
    values = dict(row) if row else {"root_id": None, "author_uid": None, "reply_to_id": None,
        "reply_to_uid": None, "text": None, "media_json": None, "platform_created_at": None,
        "public_seen_at": None, "own_list_seen_at": None, "receipt_seen_at": None}
    updates = {"root_id": identifier(node.get("root_id")), "author_uid": identifier((node.get("author") or {}).get("uid")),
        "reply_to_id": identifier(node.get("reply_to_id")), "reply_to_uid": identifier(node.get("reply_to_uid")),
        "text": node.get("raw_text", node.get("text")), "platform_created_at": node.get("created_at")}
    if node.get("media") and isinstance(node["media"], list) or "media" in node.get("observed_fields", []):
        updates["media_json"] = dumps(node.get("media") or [])
    for field, value in updates.items():
        if value is None:
            continue
        if field == "text" and not isinstance(value, str):
            raise XhhConfigError("interaction text must be a string or absent")
        old = fields.get(field)
        version = node.get("updated_at")
        newer = old is None or SOURCE_RANK[source_kind] > SOURCE_RANK[old["source"]]
        if old and source_kind == old["source"]:
            old_version = old.get("platform_updated_at")
            newer = (version >= old_version if version is not None and old_version is not None else now >= old["observed_at"])
        if newer:
            values[field] = value
            fields[field] = {"source": source_kind, "observed_at": now, "platform_updated_at": version}
    key = source_kind + ":" + str(evidence.get("route", ""))
    previous = sources.get(key)
    if previous is None or now >= previous["last_observed_at"]:
        sources[key] = {**evidence, "kind": source_kind, "first_observed_at": previous["first_observed_at"] if previous else now,
                        "last_observed_at": now}
    timestamp_field = {"public": "public_seen_at", "own_list": "own_list_seen_at", "receipt": "receipt_seen_at"}.get(source_kind)
    if timestamp_field:
        values[timestamp_field] = max(now, values[timestamp_field] or now)
    connection.execute("""INSERT INTO interaction_comments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(uid,comment_id) DO UPDATE SET root_id=excluded.root_id,author_uid=excluded.author_uid,
        reply_to_id=excluded.reply_to_id,reply_to_uid=excluded.reply_to_uid,text=excluded.text,
        media_json=excluded.media_json,platform_created_at=excluded.platform_created_at,
        last_seen_at=excluded.last_seen_at,public_seen_at=excluded.public_seen_at,
        own_list_seen_at=excluded.own_list_seen_at,receipt_seen_at=excluded.receipt_seen_at,
        source_json=excluded.source_json,field_evidence_json=excluded.field_evidence_json""",
        (uid, cid, post, values["root_id"], values["author_uid"], values["reply_to_id"], values["reply_to_uid"],
         values["text"], values["media_json"], values["platform_created_at"], row["first_seen_at"] if row else now,
         max(now, row["last_seen_at"]) if row else now, values["public_seen_at"], values["own_list_seen_at"],
         values["receipt_seen_at"], dumps(sources), dumps(fields)))


def project_receipts(connection, uid):
    rows = connection.execute("""SELECT c.*,o.receipt,o.updated_at FROM interaction_write_context c
        JOIN operations o USING(uid,operation_id)
        WHERE c.uid=? AND o.status='returned' AND c.projected_at IS NULL""", (uid,)).fetchall()
    for row in rows:
        receipt = json.loads(row["receipt"])
        cid = identifier(receipt.get("comment_id")) if isinstance(receipt, dict) else None
        if not cid:
            continue
        for key, value in (("link_id", row["post_id"]), ("root_id", row["root_id"]), ("reply_id", row["reply_to_id"])):
            if receipt.get(key) is not None and str(receipt[key]) != value:
                raise XhhConfigError("receipt target conflicts with canonical context")
        payload = json.loads(row["payload_json"])
        node = {"id": cid, "post_id": row["post_id"], "root_id": row["root_id"] or cid,
                "reply_to_id": row["reply_to_id"], "author": {"uid": uid}, "text": payload["text"]}
        if payload.get("image_url"):
            node["media"] = [{"url": payload["image_url"]}]
        observe(connection, uid, node, "receipt", {"route": "operation_receipt", "observed_at": row["updated_at"],
                "operation_id": row["operation_id"], "identity_verified_this_read": False})
        connection.execute("UPDATE interaction_write_context SET projected_at=? WHERE uid=? AND operation_id=?",
                           (time.time(), uid, row["operation_id"]))
    return len(rows)


class InteractionHistory:
    def __init__(self, root: Path):
        self.path = root / "operations.sqlite3"

    def recover(self, uid):
        if not self.path.exists():
            return {"projection_complete": True}
        with database(self.path) as connection:
            if not has_schema(connection):
                return {"projection_complete": True, "legacy_targets_unknown": True}
            needed = connection.execute("""SELECT 1 FROM interaction_write_context c JOIN operations o USING(uid,operation_id)
                WHERE c.uid=? AND o.status='returned' AND c.projected_at IS NULL LIMIT 1""", (uid,)).fetchone()
        if needed:
            try:
                with database(self.path, write=True) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    project_receipts(connection, uid)
            except (sqlite3.Error, OSError, XhhConfigError, ValueError, TypeError) as error:
                return {"projection_complete": False, "projection_error": type(error).__name__}
        return {"projection_complete": True}

    def trusted(self, uid):
        if not self.path.exists():
            return False
        with database(self.path) as connection:
            return has_schema(connection) and connection.execute(
                "SELECT 1 FROM interaction_sync_state WHERE uid=? AND query_key='identity'", (uid,)).fetchone() is not None

    def establish(self, uid, evidence):
        with database(self.path, write=True) as connection:
            establish_identity(connection, uid, evidence)

    def capture(self, uid, nodes, source_kind, evidence, *, inbox=(), scope=None, coverage=None):
        if source_kind not in SOURCE_RANK:
            raise XhhConfigError("unknown interaction source")
        if source_kind in ("inbox", "own_list") and not evidence.get("identity_verified_this_read"):
            raise XhhConfigError("private history requires identity verification on this read")
        if source_kind == "public" and not evidence.get("identity_verified_this_read") and not self.trusted(uid):
            return {"persisted": False, "reason": "no_trusted_uid_namespace"}
        with database(self.path, write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if evidence.get("identity_verified_this_read"):
                establish_identity(connection, uid, {"source": source_kind, "route": evidence.get("route")})
            for node in nodes:
                observe(connection, uid, node, source_kind, node.get("source", evidence))
            for event in inbox:
                now = evidence["observed_at"]
                connection.execute("""INSERT INTO interaction_inbox VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(uid,event_key) DO UPDATE SET last_seen_at=excluded.last_seen_at,
                    raw_json=excluded.raw_json,root_id=COALESCE(excluded.root_id,interaction_inbox.root_id),
                    peer_uid=COALESCE(excluded.peer_uid,interaction_inbox.peer_uid)""",
                    (uid, event["event_key"], event["post_id"], event["incoming_comment_id"], event.get("root_id"),
                     event.get("peer_uid"), now, now, dumps(event["raw"])))
            if scope is not None and coverage is not None:
                self.save_coverage(connection, scope, source_kind, coverage, evidence["observed_at"])
        return {"persisted": True, "comments_observed": len(nodes), "identity_verified_this_read": bool(evidence.get("identity_verified_this_read"))}

    @staticmethod
    def save_coverage(connection, scope, source, coverage, checked_at):
        key = hashlib.sha256(dumps({**scope.binding(), "source": source}).encode()).hexdigest()
        connection.execute("""INSERT INTO interaction_sync_state VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(uid,query_key) DO UPDATE SET last_checked_at=excluded.last_checked_at,
            coverage_json=excluded.coverage_json""", (scope.account_uid, key, scope.post_id, scope.root_id, scope.peer_uid,
            source, checked_at, dumps({**coverage, "history_complete": False})))

    def _records(self, connection, scope):
        comments = [dict(row) for row in connection.execute("SELECT * FROM interaction_comments WHERE uid=? AND post_id=?",
                                                           (scope.account_uid, scope.post_id))]
        operations = [dict(row) for row in connection.execute("""SELECT c.*,o.status,o.receipt,o.updated_at,o.kind
            FROM interaction_write_context c JOIN operations o USING(uid,operation_id) WHERE c.uid=? AND c.post_id=?""",
            (scope.account_uid, scope.post_id))]
        events = [dict(row) for row in connection.execute("SELECT * FROM interaction_inbox WHERE uid=? AND post_id=?",
                                                         (scope.account_uid, scope.post_id))]
        coverage = [dict(row) for row in connection.execute("""SELECT * FROM interaction_sync_state WHERE uid=? AND post_id=?
            AND root_id IS ? AND peer_uid IS ?""", (scope.account_uid, scope.post_id, scope.root_id, scope.peer_uid))]
        return comments, operations, events, coverage

    def status(self, scope):
        recovery = self.recover(scope.account_uid)
        if not self.path.exists():
            return summarize(scope, [], [], [], [], recovery)
        with database(self.path) as connection:
            connection.execute("BEGIN")
            records = self._records(connection, scope) if has_schema(connection) else ([], [], [], [])
            return summarize(scope, *records, recovery)

    def history(self, scope, *, limit=20, resume=None):
        if not 1 <= limit <= 100:
            raise XhhConfigError("history limit must be within 1..100")
        after = None
        if resume:
            token = decode_resume(resume, "interaction-history", scope.account_uid)
            if set(token) != {"scope", "after"} or token["scope"] != scope.binding() or not identifier(token["after"]):
                raise XhhConfigError("history resume belongs to a different query")
            after = token["after"]
        recovery = self.recover(scope.account_uid)
        records = ([], [], [], [])
        if self.path.exists():
            with database(self.path) as connection:
                connection.execute("BEGIN")
                if has_schema(connection):
                    records = self._records(connection, scope)
        comments, operations, events, coverage = records
        selected = filter_comments(scope, comments)
        selected.sort(key=lambda row: (len(row["comment_id"]), row["comment_id"]))
        if after:
            selected = [row for row in selected if (len(row["comment_id"]), row["comment_id"]) > (len(after), after)]
        page = selected[:limit]
        more = len(selected) > limit
        return {"account_uid": scope.account_uid, **scope.binding(), "items": [history_node(row, operations) for row in page],
                "status": summarize(scope, *records, recovery),
                "coverage": {"history_complete": False, "scope": "known_records", "has_more_local": more,
                "resume": encode_resume("interaction-history", scope.account_uid, {"scope": scope.binding(), "after": page[-1]["comment_id"]}) if more else None,
                "order": "numeric_cid", **recovery}}

    def summaries(self, uid, post_ids):
        recovery = self.recover(uid)
        ids = list(dict.fromkeys(post_ids))
        grouped = {post: ([], [], [], []) for post in ids}
        if ids and self.path.exists():
            with database(self.path) as connection:
                connection.execute("BEGIN")
                if has_schema(connection):
                    # Read once per table for the feed, without expanding peer histories.
                    for start in range(0, len(ids), 400):
                        batch = ids[start:start + 400]
                        marks = ",".join("?" for _ in batch)
                        for index, sql in enumerate((f"SELECT * FROM interaction_comments WHERE uid=? AND post_id IN ({marks})",
                            f"SELECT c.*,o.status,o.receipt,o.updated_at,o.kind FROM interaction_write_context c JOIN operations o USING(uid,operation_id) WHERE c.uid=? AND c.post_id IN ({marks})",
                            f"SELECT * FROM interaction_inbox WHERE uid=? AND post_id IN ({marks})")):
                            for row in connection.execute(sql, [uid, *batch]):
                                grouped[row["post_id"]][index].append(dict(row))
        result = {}
        for post, records in grouped.items():
            status = summarize(Scope(uid, post), *records, recovery)
            result[post] = {key: status[key] for key in ("account_uid", "participation", "my_known_comment_count",
                "my_publicly_observed_comment_count", "completed_round_count", "history_complete")}
            result[post]["projection_complete"] = recovery["projection_complete"]
        return result


def filter_comments(scope, comments):
    by_id = {row["comment_id"]: row for row in comments}
    selected = []
    for row in comments:
        if scope.root_id and row["root_id"] != scope.root_id:
            continue
        parent = by_id.get(row["reply_to_id"])
        if scope.peer_uid and not (row["author_uid"] == scope.peer_uid or row["author_uid"] == scope.account_uid and
            (row["reply_to_uid"] == scope.peer_uid or parent and parent["author_uid"] == scope.peer_uid)):
            continue
        selected.append(row)
    return selected


def history_node(row, operations):
    return {"id": row["comment_id"], "post_id": row["post_id"], "root_id": row["root_id"],
            "author": {"uid": row["author_uid"]}, "reply_to_id": row["reply_to_id"], "reply_to_uid": row["reply_to_uid"],
            "text": row["text"], "media": json.loads(row["media_json"]) if row["media_json"] else [],
            "created_at": row["platform_created_at"], "first_seen_at": row["first_seen_at"], "last_seen_at": row["last_seen_at"],
            "public_seen_at": row["public_seen_at"], "own_list_seen_at": row["own_list_seen_at"], "receipt_seen_at": row["receipt_seen_at"],
            "sources": json.loads(row["source_json"]), "field_evidence": json.loads(row["field_evidence_json"]),
            "operation_ids": [op["operation_id"] for op in operations if op["receipt"] and
                              json.loads(op["receipt"]).get("comment_id") == row["comment_id"]]}


def summarize(scope, comments, operations, events, coverage, recovery):
    uid = scope.account_uid
    by_id = {row["comment_id"]: row for row in comments}
    selected = filter_comments(scope, comments)
    mine = [row for row in selected if row["author_uid"] == uid]
    public = [row for row in mine if row["public_seen_at"] is not None]
    rounds = {row["reply_to_id"] for row in public if row["reply_to_id"] in by_id and
              by_id[row["reply_to_id"]]["author_uid"] not in (None, uid)}
    def in_scope(root, peer):
        return (not scope.root_id or scope.root_id == root) and (not scope.peer_uid or scope.peer_uid == peer)
    scoped_ops = [op for op in operations if in_scope(op["root_id"],
                   (by_id.get(op["reply_to_id"]) or {}).get("author_uid"))]
    unresolved = [{"operation_id": op["operation_id"], "state": "write_uncertain", "status": op["status"],
                   "reply_to_id": op["reply_to_id"], "root_id": op["root_id"]} for op in scoped_ops if op["status"] != "returned"]
    incoming = {row["comment_id"] for row in selected if row["author_uid"] != uid and
                (row["reply_to_uid"] == uid or (by_id.get(row["reply_to_id"]) or {}).get("author_uid") == uid)}
    incoming.update(event["incoming_comment_id"] for event in events if in_scope(event["root_id"], event["peer_uid"]))
    incoming.update(op["reply_to_id"] for op in scoped_ops if op["reply_to_id"])
    pending = []
    for cid in sorted(incoming, key=lambda value: (len(value), value)):
        replies = [row for row in mine if row["reply_to_id"] == cid]
        related = [op for op in scoped_ops if op["reply_to_id"] == cid]
        uncertain = [op for op in related if op["status"] != "returned"]
        if any(row["public_seen_at"] is not None for row in replies):
            state = "answered"
        elif uncertain:
            state = "write_uncertain"
        elif any(op["status"] == "returned" for op in related) or any(row["receipt_seen_at"] is not None for row in replies):
            state = "awaiting_public_readback"
        elif replies:
            state = "replied_visibility_unverified"
        else:
            state = "awaiting_my_reply"
        node = by_id.get(cid) or {}
        pending.append({"incoming_comment_id": cid, "operation_id": "incoming-" + cid,
                        "operation_ids": [op["operation_id"] for op in related], "state": state,
                        "peer_uid": node.get("author_uid"), "root_id": node.get("root_id"),
                        "unresolved_operation_ids": [op["operation_id"] for op in uncertain]})
    peers = sorted({row["author_uid"] for row in selected if row["author_uid"] not in (None, uid)})
    peer_summaries = [{"peer_uid": peer, "known_comment_count": sum(row["author_uid"] == peer for row in selected),
        "completed_round_count": sum(by_id[cid]["author_uid"] == peer for cid in rounds)} for peer in peers]
    missing = sorted({row["reply_to_id"] for row in selected if row["reply_to_id"] and row["reply_to_id"] not in by_id})
    unknown = [row["comment_id"] for row in selected if not row["root_id"] or row["comment_id"] != row["root_id"] and not row["reply_to_id"]]
    participation = "confirmed" if any(row["public_seen_at"] is not None or row["own_list_seen_at"] is not None for row in mine) else \
        "submitted" if mine or any(op["status"] == "returned" for op in scoped_ops) else "uncertain" if unresolved else "unknown"
    return {"account_uid": uid, **scope.binding(), "participation": participation,
            "my_known_comment_count": len(mine), "my_publicly_observed_comment_count": len(public),
            "completed_round_count": len(rounds), "pending": pending, "unresolved_operations": unresolved,
            "peer_summaries": peer_summaries, "missing_ancestor_ids": missing, "unknown_parent_ids": unknown,
            "ancestry_complete": bool(selected) and not missing and not unknown,
            "history_complete": False, "coverage": {"history_complete": False, "scope": "known_records",
            "last_checked_at": max((row["last_checked_at"] for row in coverage), default=None),
            "sources": [{"source": row["source"], "last_checked_at": row["last_checked_at"],
                         **json.loads(row["coverage_json"])} for row in coverage]}, **recovery}
