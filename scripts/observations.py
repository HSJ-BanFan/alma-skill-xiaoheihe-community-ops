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


def stable(value):
    if isinstance(value, dict):
        return {key: stable(item) for key, item in value.items()
                if key not in {"source", "raw", "raw_pages", "observed_at", "budget", "content_sha256"}}
    if isinstance(value, list):
        return [stable(item) for item in value]
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(stable(value), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def record_key(item):
    identity = next((str(item[key]) for key in ("id", "uid", "topic_id", "topicid", "linkid", "appid", "folder_id")
                     if item.get(key) is not None), None)
    return identity or "content:" + fingerprint(item)


def records(data):
    items = data.get("items") if isinstance(data, dict) else None
    items = items if isinstance(items, list) else [data]
    result = {}
    for item in items:
        if not isinstance(item, dict):
            raise XhhConfigError("snapshot records must be objects")
        identity = record_key(item)
        if identity in result:
            raise XhhConfigError("snapshot contains duplicate record identities")
        result[identity] = fingerprint(item)
    return result


def difference(previous, current):
    before, after = records(previous), records(current)
    return {"new": sorted(after.keys() - before.keys()),
            "changed": sorted(key for key in after.keys() & before.keys() if after[key] != before[key]),
            "unchanged": sorted(key for key in after.keys() & before.keys() if after[key] == before[key]),
            "not_observed": sorted(before.keys() - after.keys()), "deletion_inferred": False}


def merge(previous, current):
    if not isinstance(previous.get("items"), list) or not isinstance(current.get("items"), list):
        return current
    merged = {}
    for data in (previous, current):
        for item in data["items"]:
            record = copy.deepcopy(item)
            if "source" not in record and "source" in data:
                record["source"] = data["source"]
            merged[record_key(record)] = record
    return {**current, "items": list(merged.values())}


class Store:
    def __init__(self, root):
        self.path = Path(root) / "browsing.sqlite3"

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS snapshots (
                uid TEXT NOT NULL, name TEXT NOT NULL, query_key TEXT NOT NULL,
                data TEXT NOT NULL, coverage TEXT NOT NULL, request TEXT NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY (uid,name))""")
            connection.commit()
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, uid, name, query_key, data, coverage, request):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise XhhConfigError("snapshot name must use 1-64 letters, digits, underscores or hyphens")
        records(data)
        with self.connect() as connection:
            connection.execute("""INSERT INTO snapshots VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(uid,name) DO UPDATE SET query_key=excluded.query_key,data=excluded.data,
                coverage=excluded.coverage,request=excluded.request,updated_at=excluded.updated_at""",
                (uid, name, query_key, json.dumps(data, ensure_ascii=False), json.dumps(coverage), json.dumps(request), time.time()))

    def get(self, uid, name):
        if not self.path.exists():
            raise XhhConfigError("snapshot not found")
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM snapshots WHERE uid=? AND name=?", (uid, name)).fetchone()
        if row is None:
            raise XhhConfigError("snapshot not found for selected account")
        return {"name": row["name"], "query_key": row["query_key"], "data": json.loads(row["data"]),
                "coverage": json.loads(row["coverage"]), "request": json.loads(row["request"]), "updated_at": row["updated_at"]}

    def list(self, uid):
        if not self.path.exists():
            return []
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT name,updated_at FROM snapshots WHERE uid=? ORDER BY updated_at DESC", (uid,))]
