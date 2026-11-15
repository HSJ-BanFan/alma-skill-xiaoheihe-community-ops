"""Account-scoped public browsing snapshots and conservative differences."""
from __future__ import annotations

import hashlib
import copy
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from xhh_sdk.exceptions import XhhConfigError
from documents import enrich_observation, entity_id
from local_files import protect


class SnapshotConflict(XhhConfigError):
    def __init__(self, revision):
        self.revision = revision
        super().__init__("snapshot changed concurrently; existing observations retained")


def stable(value):
    if isinstance(value, dict):
        return {key: stable(item) for key, item in value.items()
                if key not in {"source", "raw", "raw_pages", "raw_responses", "observed_at", "budget", "content_sha256"}}
    if isinstance(value, list):
        return [stable(item) for item in value]
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(stable(value), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def record_key(item):
    identity = entity_id(item)
    return identity or "content:" + fingerprint(item)


def records(data):
    return {key: fingerprint(item) for key, item in record_items(data).items()}


def record_items(data):
    items = data.get("items") if isinstance(data, dict) else None
    items = items if isinstance(items, list) else [data]
    result = {}
    for item in items:
        if not isinstance(item, dict):
            raise XhhConfigError("snapshot records must be objects")
        record = copy.deepcopy(item)
        if "source" not in record and isinstance(data, dict) and "source" in data:
            record["source"] = copy.deepcopy(data["source"])
        identity = record_key(record)
        if identity in result:
            raise XhhConfigError("snapshot contains duplicate record identities")
        result[identity] = record
    return result


def difference(previous, current):
    before, after = record_items(previous), record_items(current)
    comparable = {key: fingerprint(enrich_observation(before[key], after[key])) for key in before.keys() & after.keys()}
    return {"new": sorted(after.keys() - before.keys()),
            "changed": sorted(key for key in comparable if comparable[key] != fingerprint(before[key])),
            "unchanged": sorted(key for key in comparable if comparable[key] == fingerprint(before[key])),
            "not_observed": sorted(before.keys() - after.keys()), "deletion_inferred": False}


def merge(previous, current):
    if not isinstance(previous.get("items"), list) or not isinstance(current.get("items"), list):
        return current
    merged = record_items(previous)
    for identity, record in record_items(current).items():
        merged[identity] = enrich_observation(merged[identity], record) if identity in merged else record
    return {**current, "items": list(merged.values())}


class Store:
    def __init__(self, root):
        self.path = Path(root) / "browsing" / "snapshots.sqlite3"

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        protect(self.path.parent)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS snapshots (
                uid TEXT NOT NULL, name TEXT NOT NULL, query_key TEXT NOT NULL,
                data TEXT NOT NULL, coverage TEXT NOT NULL, request TEXT NOT NULL, updated_at REAL NOT NULL,
                revision INTEGER NOT NULL, pending_response TEXT,
                PRIMARY KEY (uid,name))""")
            connection.commit()
            protect(self.path)
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, uid, name, query_key, data, coverage, request, *, expected_revision=0, pending_response=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise XhhConfigError("snapshot name must use 1-64 letters, digits, underscores or hyphens")
        records(data)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT revision,pending_response FROM snapshots WHERE uid=? AND name=?", (uid, name)).fetchone()
            current = row["revision"] if row else 0
            if current != expected_revision or row is not None and row["pending_response"] is not None:
                raise SnapshotConflict(current)
            revision = current + 1
            connection.execute("""INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(uid,name) DO UPDATE SET query_key=excluded.query_key,data=excluded.data,
                coverage=excluded.coverage,request=excluded.request,updated_at=excluded.updated_at,
                revision=excluded.revision,pending_response=excluded.pending_response""",
                (uid, name, query_key, json.dumps(data, ensure_ascii=False), json.dumps(coverage), json.dumps(request), time.time(), revision,
                 json.dumps(pending_response, ensure_ascii=False) if pending_response is not None else None))
        return revision

    def get(self, uid, name):
        if not self.path.exists():
            raise XhhConfigError("snapshot not found")
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM snapshots WHERE uid=? AND name=?", (uid, name)).fetchone()
        if row is None:
            raise XhhConfigError("snapshot not found for selected account")
        return {"name": row["name"], "query_key": row["query_key"], "data": json.loads(row["data"]),
                "coverage": json.loads(row["coverage"]), "request": json.loads(row["request"]), "updated_at": row["updated_at"],
                "revision": row["revision"], "pending_response": json.loads(row["pending_response"]) if row["pending_response"] else None}

    def optional(self, uid, name):
        if not self.path.exists():
            return None
        with self.connect() as connection:
            exists = connection.execute("SELECT 1 FROM snapshots WHERE uid=? AND name=?", (uid, name)).fetchone()
        return self.get(uid, name) if exists else None

    def pending_by_name(self, name):
        if not isinstance(name, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,64}", name):
            raise XhhConfigError(
                "snapshot name must use 1-64 letters, digits, underscores or hyphens")
        if not self.path.exists():
            raise XhhConfigError("snapshot not found")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT uid FROM snapshots "
                "WHERE name=? AND pending_response IS NOT NULL",
                (name,),
            ).fetchall()
        if not rows:
            raise XhhConfigError("pending snapshot not found")
        if len(rows) != 1:
            raise XhhConfigError(
                "pending snapshot name belongs to more than one account",
                code="snapshot_account_required",
                fix="select --account or --config before resuming this snapshot",
            )
        uid = rows[0]["uid"]
        return uid, self.get(uid, name)

    def delivered(self, uid, name, revision):
        with self.connect() as connection:
            connection.execute("UPDATE snapshots SET pending_response=NULL WHERE uid=? AND name=? AND revision=?", (uid, name, revision))

    def list(self, uid):
        if not self.path.exists():
            return []
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT name,updated_at FROM snapshots WHERE uid=? ORDER BY updated_at DESC", (uid,))]
