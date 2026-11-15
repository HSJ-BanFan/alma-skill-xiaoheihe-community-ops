"""UID-scoped interaction facts in the canonical operation database."""
from __future__ import annotations

import hashlib
import json
import math
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
    post_id: str | None
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
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise XhhConfigError("interaction database version is newer than this tool")
    return version == SCHEMA_VERSION


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
        platform_edit = old and field in ("text", "media_json") and source_kind in ("public", "own_list") and old["source"] in ("public", "own_list")
        if old and (source_kind == old["source"] or platform_edit):
            old_version = old.get("platform_updated_at")
            newer = (version >= old_version if version is not None and old_version is not None else now >= old["observed_at"])
            if field == "platform_created_at":
                newer = False
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
            raise XhhConfigError("returned comment receipt lacks a reliable CID")
        for key, value in (("link_id", row["post_id"]), ("root_id", row["root_id"]), ("reply_id", row["reply_to_id"])):
            if receipt.get(key) is not None and str(receipt[key]) != value:
                raise XhhConfigError("receipt target conflicts with canonical context")
        payload = json.loads(row["payload_json"])
        node = {"id": cid, "post_id": row["post_id"], "root_id": row["root_id"] or cid,
                "reply_to_id": row["reply_to_id"], "author": {"uid": uid}, "text": payload["text"]}
        image = (payload.get("submitted_request") or {}).get("imgs") or payload.get("image_url")
        if image:
            node["media"] = [{"url": image}]
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
            return {"projection_complete": True, "identity_established": False}
        with database(self.path) as connection:
            if not has_schema(connection):
                return {"projection_complete": True, "legacy_targets_unknown": True, "identity_established": False}
            identity = connection.execute("SELECT last_checked_at FROM interaction_sync_state WHERE uid=? AND query_key='identity'", (uid,)).fetchone()
            identity_evidence = {"identity_established": identity is not None,
                                 "identity_last_verified_at": identity["last_checked_at"] if identity else None}
            needed = connection.execute("""SELECT 1 FROM interaction_write_context c JOIN operations o USING(uid,operation_id)
                WHERE c.uid=? AND o.status='returned' AND c.projected_at IS NULL LIMIT 1""", (uid,)).fetchone()
        if needed:
            try:
                with database(self.path, write=True) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    project_receipts(connection, uid)
            except Exception as error:
                return {"projection_complete": False, "projection_error": type(error).__name__, **identity_evidence}
        return {"projection_complete": True, **identity_evidence}

    def trusted(self, uid):
        if not self.path.exists():
            return False
        with database(self.path) as connection:
            return has_schema(connection) and connection.execute(
                "SELECT 1 FROM interaction_sync_state WHERE uid=? AND query_key='identity'", (uid,)).fetchone() is not None

    def root_post(self, uid, root_id):
        if not self.path.exists():
            return None
        with database(self.path) as connection:
            if not has_schema(connection):
                return None
            row = connection.execute("SELECT post_id FROM interaction_comments WHERE uid=? AND comment_id=? AND root_id=?",
                                     (uid, root_id, root_id)).fetchone()
            return row["post_id"] if row else None

    def establish(self, uid, evidence):
        with database(self.path, write=True) as connection:
            establish_identity(connection, uid, evidence)

    def capture(self, uid, nodes, source_kind, evidence, *, inbox=(), scope=None, coverage=None):
        if source_kind not in ("public", "inbox", "own_list"):
            raise XhhConfigError("read capture requires a read source; receipts use canonical operation projection")
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
                existing = connection.execute("SELECT post_id,incoming_comment_id FROM interaction_inbox WHERE uid=? AND event_key=?",
                                              (uid, event["event_key"])).fetchone()
                if existing and (existing["post_id"] != event["post_id"] or existing["incoming_comment_id"] != event["incoming_comment_id"]):
                    raise XhhConfigError("inbox event identity conflicts with previous observation")
                connection.execute("""INSERT INTO interaction_inbox VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(uid,event_key) DO UPDATE SET last_seen_at=excluded.last_seen_at,
                    raw_json=excluded.raw_json,root_id=COALESCE(excluded.root_id,interaction_inbox.root_id),
                    peer_uid=COALESCE(excluded.peer_uid,interaction_inbox.peer_uid)""",
                    (uid, event["event_key"], event["post_id"], event["incoming_comment_id"], event.get("root_id"),
                     event.get("peer_uid"), now, now, dumps(event["raw"])))
            if scope is not None and coverage is not None:
                self.save_coverage(connection, scope, source_kind, coverage, evidence["observed_at"])
            elif source_kind in ("inbox", "own_list") and coverage is not None:
                self.save_coverage(connection, Scope(uid, None), source_kind,
                                   {**coverage, "scope": "account_page"}, evidence["observed_at"])
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
        coverage = [dict(row) for row in connection.execute("""SELECT * FROM interaction_sync_state WHERE uid=? AND
            ((post_id=? AND root_id IS ? AND peer_uid IS ?) OR
             (post_id IS NULL AND root_id IS NULL AND peer_uid IS NULL AND source IN ('inbox','own_list')))""",
            (scope.account_uid, scope.post_id, scope.root_id, scope.peer_uid))]
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
            position = token.get("after")
            if set(token) != {"scope", "after", "order"} or token["scope"] != scope.binding() or token["order"] != "activity_time_desc" or not isinstance(position, dict) or set(position) != {"time", "cid"}:
                raise XhhConfigError("history resume belongs to a different query")
            cid, timestamp = identifier(position["cid"]), position["time"]
            if not cid or isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
                raise XhhConfigError("invalid history continuation position")
            after = (timestamp, len(cid), cid)
        recovery = self.recover(scope.account_uid)
        records = ([], [], [], [])
        if self.path.exists():
            with database(self.path) as connection:
                connection.execute("BEGIN")
                if has_schema(connection):
                    records = self._records(connection, scope)
        comments, operations, events, coverage = records
        participants = {row["comment_id"] for row in filter_comments(scope, comments)}
        selected = filter_comments(scope, comments, include_ancestors=True)
        selected.sort(key=history_position, reverse=True)
        if after:
            selected = [row for row in selected if history_position(row) < after]
        page = selected[:limit]
        more = len(selected) > limit
        items = [{**history_node(row, operations), "context_only": row["comment_id"] not in participants} for row in page]
        position = {"time": history_position(page[-1])[0], "cid": page[-1]["comment_id"]} if more else None
        return {"account_uid": scope.account_uid, **scope.binding(), "items": items,
                "status": summarize(scope, *records, recovery),
                "coverage": {"history_complete": False, "scope": "known_records", "has_more_local": more,
                "resume": encode_resume("interaction-history", scope.account_uid, {"scope": scope.binding(), "after": position, "order": "activity_time_desc"}) if more else None,
                "order": "activity_time_desc", **recovery}}

    def summaries(self, uid, post_ids):
        recovery = self.recover(uid)
        ids = list(dict.fromkeys(post_ids))
        grouped = {post: ([], [], [], []) for post in ids}
        if ids and self.path.exists():
            with database(self.path) as connection:
                connection.execute("BEGIN")
                if has_schema(connection):
                    for start in range(0, len(ids), 400):
                        batch = ids[start:start + 400]
                        marks = ",".join("?" for _ in batch)
                        for index, sql in enumerate((f"""SELECT uid,comment_id,post_id,root_id,author_uid,reply_to_id,reply_to_uid,
                            public_seen_at,own_list_seen_at,receipt_seen_at,field_evidence_json
                            FROM interaction_comments WHERE uid=? AND post_id IN ({marks})""",
                            f"""SELECT c.operation_id,c.post_id,c.root_id,c.reply_to_id,o.status FROM interaction_write_context c
                            JOIN operations o USING(uid,operation_id) WHERE c.uid=? AND c.post_id IN ({marks})""",
                            f"SELECT post_id,incoming_comment_id,root_id,peer_uid FROM interaction_inbox WHERE uid=? AND post_id IN ({marks})")):
                            for row in connection.execute(sql, [uid, *batch]):
                                grouped[row["post_id"]][index].append(dict(row))
        result = {}
        for post, records in grouped.items():
            status = summarize(Scope(uid, post), *records, recovery)
            result[post] = {key: status[key] for key in ("account_uid", "participation", "my_known_comment_count",
                "my_publicly_observed_comment_count", "completed_round_count", "history_complete")}
            result[post]["projection_complete"] = recovery["projection_complete"]
            result[post]["identity_established"] = recovery["identity_established"]
        return result


def filter_comments(scope, comments, *, include_ancestors=False):
    by_id = {row["comment_id"]: row for row in comments}
    own_seeds = {row["reply_to_id"] for row in comments if scope.peer_uid and row["author_uid"] == scope.peer_uid
                 and (not scope.root_id or row["root_id"] == scope.root_id)
                 and (by_id.get(row["reply_to_id"]) or {}).get("author_uid") == scope.account_uid}
    selected = []
    for row in comments:
        parent = by_id.get(row["reply_to_id"])
        known_root = row["root_id"] or (parent or {}).get("root_id")
        if scope.root_id and known_root != scope.root_id:
            continue
        if scope.peer_uid and not (row["author_uid"] == scope.peer_uid or row["author_uid"] == scope.account_uid and
            (((parent or {}).get("author_uid") or row["reply_to_uid"]) == scope.peer_uid or row["comment_id"] in own_seeds)):
            continue
        selected.append(row)
    if include_ancestors:
        found = {row["comment_id"]: row for row in selected}
        pending = list(selected)
        while pending:
            parent = by_id.get(pending.pop()["reply_to_id"])
            if parent is None or parent["comment_id"] in found:
                continue
            if scope.root_id and parent["root_id"] not in (None, scope.root_id):
                continue
            found[parent["comment_id"]] = parent
            pending.append(parent)
        selected = list(found.values())
    return selected


def history_clock(row):
    for field in ("platform_created_at", "receipt_seen_at", "first_seen_at"):
        timestamp = row.get(field)
        if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) and math.isfinite(timestamp):
            return timestamp, field
    return 0, "unknown"


def history_position(row):
    return history_clock(row)[0], len(row["comment_id"]), row["comment_id"]


def history_node(row, operations):
    return {"id": row["comment_id"], "post_id": row["post_id"], "root_id": row["root_id"],
            "author": {"uid": row["author_uid"]}, "reply_to_id": row["reply_to_id"], "reply_to_uid": row["reply_to_uid"],
            "text": row["text"], "media": json.loads(row["media_json"]) if row["media_json"] else [],
            "created_at": row["platform_created_at"], "first_seen_at": row["first_seen_at"], "last_seen_at": row["last_seen_at"],
            "order_time": history_clock(row)[0], "order_time_source": history_clock(row)[1],
            "public_seen_at": row["public_seen_at"], "own_list_seen_at": row["own_list_seen_at"], "receipt_seen_at": row["receipt_seen_at"],
            "sources": json.loads(row["source_json"]), "field_evidence": json.loads(row["field_evidence_json"]),
            "operation_ids": [op["operation_id"] for op in operations if receipt_cid(op["receipt"]) == row["comment_id"]]}


def receipt_cid(serialized):
    value = json.loads(serialized) if serialized else None
    return identifier(value.get("comment_id")) if isinstance(value, dict) else None


def summarize(scope, comments, operations, events, coverage, recovery):
    uid = scope.account_uid
    by_id = {row["comment_id"]: row for row in comments}
    selected = filter_comments(scope, comments)
    mine = [row for row in selected if row["author_uid"] == uid]
    public = [row for row in mine if public_fields(row, "author_uid")]
    rounds = {row["reply_to_id"] for row in public if public_fields(row, "reply_to_id") and row["reply_to_id"] in by_id and
              by_id[row["reply_to_id"]]["author_uid"] not in (None, uid) and
              (not scope.peer_uid or by_id[row["reply_to_id"]]["author_uid"] == scope.peer_uid) and
              (not scope.root_id or by_id[row["reply_to_id"]]["root_id"] == scope.root_id)}
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
    incoming.update(row["reply_to_id"] for row in mine if row["reply_to_id"] and
                    (not scope.peer_uid or ((by_id.get(row["reply_to_id"]) or {}).get("author_uid") or row["reply_to_uid"]) == scope.peer_uid))
    pending = []
    for cid in sorted(incoming, key=lambda value: (len(value), value)):
        replies = [row for row in mine if row["reply_to_id"] == cid]
        related = [op for op in scoped_ops if op["reply_to_id"] == cid]
        uncertain = [op for op in related if op["status"] != "returned"]
        if any(public_fields(row, "author_uid", "reply_to_id") for row in replies):
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
        known_root = node.get("root_id") or next((op["root_id"] for op in related if op["root_id"]), None) or \
            next((row["root_id"] for row in replies if row["root_id"]), None)
        known_peer = node.get("author_uid") or next((row["reply_to_uid"] for row in replies if row["reply_to_uid"]), None)
        pending.append({"incoming_comment_id": cid, "operation_id": "incoming-" + cid,
                        "operation_ids": [op["operation_id"] for op in related], "state": state,
                        "peer_uid": known_peer, "root_id": known_root,
                        "unresolved_operation_ids": [op["operation_id"] for op in uncertain]})
    peers = sorted({row["author_uid"] for row in selected if row["author_uid"] not in (None, uid)})
    peer_summaries = [{"peer_uid": peer, "known_comment_count": sum(row["author_uid"] == peer for row in selected),
        "my_known_reply_count": sum((by_id.get(row["reply_to_id"]) or {}).get("author_uid", row["reply_to_uid"]) == peer for row in mine),
        "my_publicly_observed_reply_count": sum(public_fields(row, "reply_to_id") and
            (by_id.get(row["reply_to_id"]) or {}).get("author_uid") == peer for row in public),
        "completed_round_count": sum(by_id[cid]["author_uid"] == peer for cid in rounds)} for peer in peers]
    missing = {row["reply_to_id"] for row in selected if row["reply_to_id"] and row["reply_to_id"] not in by_id}
    unknown = {row["comment_id"] for row in selected if not row["root_id"] or row["comment_id"] != row["root_id"] and not row["reply_to_id"]}
    cyclic, root_mismatches = set(), set()
    for row in selected:
        current, visited = row, set()
        while current and current["comment_id"] != row["root_id"]:
            if current["comment_id"] in visited:
                cyclic.update(visited)
                break
            visited.add(current["comment_id"])
            if current["comment_id"] == current["root_id"]:
                root_mismatches.add(row["comment_id"])
                break
            if current["reply_to_id"] is None:
                unknown.add(current["comment_id"])
                break
            if current["reply_to_id"] not in by_id:
                missing.add(current["reply_to_id"])
            current = by_id.get(current["reply_to_id"])
    participation = "confirmed" if public or any(row["own_list_seen_at"] is not None for row in mine) else \
        "submitted" if mine or any(op["status"] == "returned" for op in scoped_ops) else "uncertain" if unresolved else "unknown"
    return {"account_uid": uid, **scope.binding(), "participation": participation,
            "my_known_comment_count": len(mine), "my_publicly_observed_comment_count": len(public),
            "completed_round_count": len(rounds), "pending": pending, "unresolved_operations": unresolved,
            "peer_summaries": peer_summaries, "missing_ancestor_ids": sorted(missing), "unknown_parent_ids": sorted(unknown),
            "cyclic_reply_ids": sorted(cyclic), "unknown_author_ids": [row["comment_id"] for row in selected if row["author_uid"] is None],
            "root_mismatch_ids": sorted(root_mismatches),
            "ancestry_complete": bool(selected) and not missing and not unknown and not cyclic and not root_mismatches,
            "history_complete": False, "coverage": {"history_complete": False, "scope": "known_records",
            "last_checked_at": max((row["last_checked_at"] for row in coverage), default=None),
            "sources": [{"source": row["source"], "last_checked_at": row["last_checked_at"],
                         **json.loads(row["coverage_json"])} for row in coverage]}, **recovery}


def public_fields(row, *names):
    fields = json.loads(row["field_evidence_json"])
    return row["public_seen_at"] is not None and all(fields.get(name, {}).get("source") == "public" for name in names)
