from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from corpus_model import (
    AttemptToken,
    CorpusManifest,
    PageEvidence,
    PageResult,
    Position,
    ProviderItem,
    StopDecision,
    canonical_json,
    merge_content,
    observation_hash,
    sha256_value,
)
from local_files import protect
from observations import Store
from xhh_sdk.exceptions import XhhConfigError

SCHEMA_VERSION = 2
MAX_LEASE_SECONDS = 3600
MAX_INVOCATION_SECONDS = 120
MAX_INVOCATION_CAP = 1_000_000
SCHEMA_TABLES = frozenset({
    "corpus_schema_meta", "corpus_runs", "corpus_queries",
    "corpus_attempts", "corpus_frames", "corpus_frame_items",
    "corpus_entities", "corpus_memberships", "corpus_deliveries",
    "corpus_page_stages",
})

PAGE_STAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS corpus_page_stages (
    run_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    frame_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending','committed')),
    page_json TEXT NOT NULL,
    staged_at REAL NOT NULL,
    committed_at REAL,
    PRIMARY KEY (run_id, attempt_id),
    UNIQUE (run_id, frame_key),
    FOREIGN KEY (run_id, query_id)
        REFERENCES corpus_queries(run_id, query_id) ON DELETE CASCADE,
    FOREIGN KEY (attempt_id) REFERENCES corpus_attempts(attempt_id)
        ON DELETE CASCADE
);
"""

SCHEMA = """
CREATE TABLE corpus_schema_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL
);
CREATE TABLE corpus_runs (
    run_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    account_uid TEXT NOT NULL,
    reader_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL,
    current_query_id TEXT,
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL,
    lease_expires_at REAL,
    lease_started_at REAL,
    seconds_used_ms INTEGER NOT NULL DEFAULT 0,
    invocation_reserved_ms INTEGER NOT NULL DEFAULT 0,
    last_stop_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE corpus_queries (
    run_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    manifest_ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    query_sha256 TEXT NOT NULL,
    sanitized_argv_json TEXT NOT NULL,
    page_limit INTEGER NOT NULL,
    new_record_limit INTEGER NOT NULL,
    state TEXT NOT NULL,
    current_position_json TEXT NOT NULL,
    next_position_json TEXT,
    pending_remainder_frame_key TEXT,
    stop_reason TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (run_id, query_id),
    UNIQUE (run_id, manifest_ordinal),
    FOREIGN KEY (run_id) REFERENCES corpus_runs(run_id) ON DELETE CASCADE
);
CREATE TABLE corpus_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    query_id TEXT,
    frame_key TEXT,
    invocation_id TEXT NOT NULL,
    lease_generation INTEGER NOT NULL,
    role TEXT NOT NULL,
    protocol TEXT NOT NULL,
    reserved_ms INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('reserved','succeeded','failed','abandoned')),
    started_at REAL NOT NULL,
    settled_at REAL,
    error_json TEXT,
    FOREIGN KEY (run_id) REFERENCES corpus_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, query_id)
        REFERENCES corpus_queries(run_id, query_id) ON DELETE CASCADE
);
CREATE TABLE corpus_frames (
    run_id TEXT NOT NULL,
    frame_key TEXT NOT NULL,
    query_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    current_position_json TEXT NOT NULL,
    next_position_json TEXT,
    provider_page_size INTEGER NOT NULL,
    coverage_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    observed_at REAL NOT NULL,
    commit_revision INTEGER NOT NULL,
    PRIMARY KEY (run_id, frame_key),
    UNIQUE (run_id, query_id, current_position_json,
            provider_page_size, commit_revision),
    FOREIGN KEY (run_id, query_id)
        REFERENCES corpus_queries(run_id, query_id) ON DELETE CASCADE,
    FOREIGN KEY (attempt_id) REFERENCES corpus_attempts(attempt_id)
);
CREATE TABLE corpus_frame_items (
    run_id TEXT NOT NULL,
    frame_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    entity_key TEXT,
    item_digest TEXT NOT NULL,
    eligibility TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (
        disposition IN ('admitted','known','deferred','invalid')),
    reason TEXT,
    PRIMARY KEY (run_id, frame_key, ordinal),
    FOREIGN KEY (run_id, frame_key)
        REFERENCES corpus_frames(run_id, frame_key) ON DELETE CASCADE
);
CREATE TABLE corpus_entities (
    run_id TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    admission_state TEXT NOT NULL CHECK (
        admission_state IN ('staged','admitted')),
    admitting_query_id TEXT,
    admission_revision INTEGER,
    first_frame_key TEXT NOT NULL,
    last_frame_key TEXT NOT NULL,
    PRIMARY KEY (run_id, entity_key),
    FOREIGN KEY (run_id, admitting_query_id)
        REFERENCES corpus_queries(run_id, query_id)
);
CREATE TABLE corpus_memberships (
    run_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    first_frame_key TEXT NOT NULL,
    last_frame_key TEXT NOT NULL,
    first_revision INTEGER NOT NULL,
    last_revision INTEGER NOT NULL,
    PRIMARY KEY (run_id, query_id, entity_key),
    FOREIGN KEY (run_id, query_id)
        REFERENCES corpus_queries(run_id, query_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, entity_key)
        REFERENCES corpus_entities(run_id, entity_key) ON DELETE CASCADE
);
CREATE TABLE corpus_deliveries (
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    delivery_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('pending','acknowledged')),
    frame_refs_json TEXT NOT NULL,
    entity_refs_json TEXT NOT NULL,
    membership_refs_json TEXT NOT NULL,
    counts_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    stop_json TEXT,
    created_at REAL NOT NULL,
    acknowledged_at REAL,
    PRIMARY KEY (run_id, revision),
    FOREIGN KEY (run_id) REFERENCES corpus_runs(run_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX corpus_one_pending_delivery
ON corpus_deliveries(run_id) WHERE state='pending';
""" + PAGE_STAGE_SCHEMA


class CorpusConflict(XhhConfigError):
    code = "corpus_conflict"


class CorpusLocked(XhhConfigError):
    code = "corpus_run_locked"


class CorpusPaused(XhhConfigError):
    code = "corpus_budget_paused"


def _bounded_number(value, name, minimum, maximum):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not minimum <= value <= maximum
    ):
        raise XhhConfigError(
            f"corpus {name} must be within {minimum}..{maximum}")
    return float(value)


def _bounded_cap(value, name):
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_INVOCATION_CAP:
        raise XhhConfigError(
            f"corpus {name} must be an integer within "
            f"0..{MAX_INVOCATION_CAP}")
    return value


class CorpusStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.path = self.root / "browsing" / "snapshots.sqlite3"

    def _ensure_parent(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        protect(self.path.parent)

    def initialize(self) -> None:
        self._ensure_parent()
        legacy = Store(self.root)
        with legacy.connect() as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='corpus_schema_meta'").fetchone()
            if row is not None:
                version = connection.execute(
                    "SELECT version FROM corpus_schema_meta "
                    "WHERE singleton=1").fetchone()
                if version is None:
                    raise XhhConfigError(
                        "corpus schema version is unsupported")
                if version["version"] == 1:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        connection.execute(PAGE_STAGE_SCHEMA)
                        connection.execute(
                            "UPDATE corpus_schema_meta SET version=? "
                            "WHERE singleton=1", (SCHEMA_VERSION,))
                    except Exception:
                        connection.rollback()
                        raise
                    connection.commit()
                elif version["version"] != SCHEMA_VERSION:
                    raise XhhConfigError(
                        "corpus schema version is unsupported")
                self._validate_schema(connection)
                connection.execute(
                    """UPDATE corpus_queries
                       SET pending_remainder_frame_key=NULL
                       WHERE pending_remainder_frame_key IS NOT NULL
                         AND state IN ('complete','quota_reached')"""
                )
                connection.execute(
                    """UPDATE corpus_page_stages SET page_json=''
                       WHERE state='committed' AND page_json<>''
                         AND EXISTS (
                           SELECT 1 FROM corpus_frames frame
                           WHERE frame.run_id=corpus_page_stages.run_id
                             AND frame.frame_key=corpus_page_stages.frame_key
                             AND frame.query_id=corpus_page_stages.query_id
                             AND frame.attempt_id=corpus_page_stages.attempt_id
                         )"""
                )
                connection.commit()
                return
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(
                    "INSERT INTO corpus_schema_meta(singleton,version) "
                    "VALUES (1,?)", (SCHEMA_VERSION,))
            except Exception:
                connection.rollback()
                raise
            connection.commit()
        protect(self.path)

    @staticmethod
    def _validate_schema(connection):
        tables = {
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'corpus_%'")
        }
        missing = SCHEMA_TABLES - tables
        if missing:
            raise XhhConfigError(
                "corpus schema is incomplete: " + ", ".join(sorted(missing)))

    @contextmanager
    def _connect_rw(self):
        self.initialize()
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def plan(self, manifest: CorpusManifest) -> dict:
        digest = manifest.sha256()
        manifest_json = canonical_json(manifest.canonical())
        now = time.time()
        with self._connect_rw() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM corpus_runs WHERE run_id=?",
                (manifest.run_id,)).fetchone()
            if row is not None:
                if row["manifest_sha256"] != digest:
                    raise CorpusConflict(
                        "run ID is already bound to another manifest")
                return self.status(manifest.run_id)
            connection.execute(
                """INSERT INTO corpus_runs(
                    run_id,schema_version,manifest_sha256,manifest_json,
                    account_alias,account_uid,reader_hash,state,revision,
                    current_query_id,lease_generation,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    manifest.run_id, manifest.schema_version, digest,
                    manifest_json, manifest.account_alias, manifest.account_uid,
                    manifest.reader.sha256(), "planned", 0, None, 0, now, now,
                ),
            )
            for ordinal, query in enumerate(manifest.queries):
                connection.execute(
                    """INSERT INTO corpus_queries(
                        run_id,query_id,manifest_ordinal,kind,query_sha256,
                        sanitized_argv_json,page_limit,new_record_limit,state,
                        current_position_json,next_position_json,
                        created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        manifest.run_id, query.query_id, ordinal, query.kind,
                        query.sha256(),
                        canonical_json(_sanitized_query(query)),
                        query.limits.pages, query.limits.new_records,
                        "pending",
                        canonical_json({"mode": "offset", "value": 0}),
                        canonical_json({"mode": "offset", "value": 0}),
                        now, now,
                    ),
                )
            connection.commit()
        return self.status(manifest.run_id)

    @contextmanager
    def _connect_ro(self):
        if not self.path.exists():
            raise XhhConfigError("corpus run not found")
        uri = self.path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _active_reserved_ms(
        connection,
        run_id,
        lease_generation,
        started_at,
        expires_at,
        now,
        reserved_ms,
    ):
        intervals = connection.execute(
            """SELECT started_at,settled_at FROM corpus_attempts
               WHERE run_id=? AND lease_generation=?""",
            (run_id, lease_generation),
        )
        active_ms = 0.0
        limit = min(now, expires_at) if expires_at is not None else now
        for attempt in intervals:
            begin = max(float(started_at or 0), float(attempt["started_at"]))
            end = min(
                limit,
                float(attempt["settled_at"])
                if attempt["settled_at"] is not None else limit,
            )
            if end > begin:
                active_ms += (end - begin) * 1000
        return min(reserved_ms, max(0, int(active_ms)))

    def manifest(self, run_id: str) -> CorpusManifest:
        with self._connect_ro() as connection:
            row = connection.execute(
                "SELECT * FROM corpus_runs WHERE run_id=?",
                (run_id,)).fetchone()
        return self._manifest_from_row(row)

    @staticmethod
    def _manifest_from_row(row) -> CorpusManifest:
        if row is None:
            raise XhhConfigError("corpus run not found")
        return CorpusManifest._from_persisted_json(
            row["manifest_json"], row["manifest_sha256"], row["reader_hash"])

    @staticmethod
    def _bound_queries(connection, run_id, manifest):
        queries = list(connection.execute(
            "SELECT * FROM corpus_queries WHERE run_id=? "
            "ORDER BY manifest_ordinal", (run_id,)))
        invalid = CorpusConflict(
            "corpus query rows do not match the manifest",
            code="corpus_query_binding_invalid",
        )
        if len(queries) != len(manifest.queries):
            raise invalid
        for ordinal, (row, query) in enumerate(zip(queries, manifest.queries)):
            if (
                row["manifest_ordinal"] != ordinal
                or row["query_id"] != query.query_id
                or row["kind"] != query.kind
                or row["query_sha256"] != query.sha256()
            ):
                raise invalid
        return queries

    @staticmethod
    def _request_usage(connection, run_id, *, invocation_id=None):
        if invocation_id is None:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM corpus_attempts "
                "WHERE run_id=?", (run_id,)).fetchone()
        else:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM corpus_attempts "
                "WHERE run_id=? AND invocation_id=?",
                (run_id, invocation_id)).fetchone()
        return row["count"]

    def status(self, run_id: str) -> dict:
        with self._connect_ro() as connection:
            run = connection.execute(
                "SELECT * FROM corpus_runs WHERE run_id=?",
                (run_id,)).fetchone()
            if run is None:
                raise XhhConfigError("corpus run not found")
            manifest = self._manifest_from_row(run)
            queries = self._bound_queries(connection, run_id, manifest)
            requests = self._request_usage(connection, run_id)
            pages = connection.execute(
                "SELECT COUNT(*) AS count FROM corpus_frames "
                "WHERE run_id=?", (run_id,)).fetchone()["count"]
            unique = connection.execute(
                "SELECT COUNT(*) AS count FROM corpus_entities "
                "WHERE run_id=? AND admission_state='admitted'",
                (run_id,)).fetchone()["count"]
            pending = connection.execute(
                "SELECT delivery_id,revision FROM corpus_deliveries "
                "WHERE run_id=? AND state='pending'", (run_id,)).fetchone()
        query_values = []
        for row, query in zip(queries, manifest.queries):
            pending_count = 0
            if (
                row["pending_remainder_frame_key"]
                and row["state"] not in {"complete", "quota_reached"}
            ):
                with self._connect_ro() as connection:
                    pending_count = connection.execute(
                        "SELECT COUNT(*) AS count FROM corpus_frame_items "
                        "WHERE run_id=? AND frame_key=? AND disposition='deferred'",
                        (run_id, row["pending_remainder_frame_key"]),
                    ).fetchone()["count"]
            with self._connect_ro() as connection:
                pages_used = connection.execute(
                    "SELECT COUNT(*) AS count FROM corpus_frames "
                    "WHERE run_id=? AND query_id=?",
                    (run_id, row["query_id"]),
                ).fetchone()["count"]
                new_records = connection.execute(
                    """SELECT COUNT(*) AS count FROM corpus_entities
                       WHERE run_id=? AND admission_state='admitted'
                         AND admitting_query_id=?""",
                    (run_id, row["query_id"]),
                ).fetchone()["count"]
            query_values.append({
                "query_id": row["query_id"],
                "kind": row["kind"],
                "state": row["state"],
                "current_position": json.loads(row["current_position_json"]),
                "next_position": (
                    json.loads(row["next_position_json"])
                    if row["next_position_json"] else None
                ),
                "page_limit": query.limits.pages,
                "pages_used": pages_used,
                "new_record_limit": query.limits.new_records,
                "new_records_used": new_records,
                "pending_remainder_count": pending_count,
                "stop_reason": row["stop_reason"],
            })
        return {
            "run": {
                "run_id": run["run_id"],
                "state": run["state"],
                "revision": run["revision"],
                "manifest_sha256": run["manifest_sha256"],
                "account_alias": run["account_alias"],
                "account_uid": run["account_uid"],
                "current_query_id": run["current_query_id"],
                "lease_active": bool(
                    run["lease_owner"] and run["lease_expires_at"]
                    and run["lease_expires_at"] > time.time()),
                "last_stop": (
                    json.loads(run["last_stop_json"])
                    if run["last_stop_json"] else None
                ),
            },
            "queries": query_values,
            "budget": {
                "requests": {"used": requests,
                             "limit": manifest.limits.requests},
                "seconds": {
                    "used": round(run["seconds_used_ms"] / 1000, 3),
                    "limit": manifest.limits.seconds,
                },
                "unique_records": {
                    "used": unique,
                    "limit": manifest.limits.unique_records,
                },
                "pages": pages,
            },
            "pending_delivery": (
                {"delivery_id": pending["delivery_id"],
                 "revision": pending["revision"]}
                if pending else None
            ),
        }

    def acquire_lease(
        self,
        run_id: str,
        owner: str,
        *,
        lease_seconds: float = 60,
        max_requests: int | None = None,
        max_seconds: float | None = None,
        max_new_records: int | None = None,
        acknowledge_block: bool = False,
    ) -> dict:
        if not owner:
            raise XhhConfigError("corpus lease requires an owner")
        lease_seconds = _bounded_number(
            lease_seconds, "lease_seconds", 1e-9, MAX_LEASE_SECONDS)
        max_seconds = (
            None if max_seconds is None else
            _bounded_number(
                max_seconds, "max_seconds", 0, MAX_INVOCATION_SECONDS)
        )
        max_requests = _bounded_cap(max_requests, "max_requests")
        max_new_records = _bounded_cap(max_new_records, "max_new_records")
        now = time.time()
        with self._connect_rw() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM corpus_runs WHERE run_id=?",
                (run_id,)).fetchone()
            if run is None:
                raise XhhConfigError("corpus run not found")
            pending = connection.execute(
                "SELECT 1 FROM corpus_deliveries "
                "WHERE run_id=? AND state='pending'", (run_id,)).fetchone()
            pending_stage = connection.execute(
                "SELECT 1 FROM corpus_page_stages "
                "WHERE run_id=? AND state='pending'", (run_id,)).fetchone()
            pending_remainder = connection.execute(
                """SELECT 1 FROM corpus_queries query
                   WHERE query.run_id=?
                     AND query.pending_remainder_frame_key IS NOT NULL
                     AND query.state NOT IN ('complete','quota_reached')
                     AND EXISTS (
                       SELECT 1 FROM corpus_frame_items item
                       WHERE item.run_id=query.run_id
                         AND item.frame_key=query.pending_remainder_frame_key
                         AND item.disposition='deferred'
                     )""",
                (run_id,),
            ).fetchone()
            replayable = bool(pending or pending_stage or pending_remainder)
            replay_only = bool(pending)
            if run["state"] == "complete" and not replayable:
                raise CorpusLocked("corpus run is already complete")
            if run["state"] == "failed" and not replayable:
                raise CorpusLocked("failed corpus runs cannot resume")
            if (run["state"] == "blocked_human" and not acknowledge_block
                    and not replayable):
                raise CorpusLocked(
                    "blocked corpus run requires explicit acknowledgement")
            if (run["lease_owner"] and run["lease_expires_at"] is not None
                    and run["lease_expires_at"] > now
                    and run["lease_owner"] != owner):
                raise CorpusLocked("corpus run is owned by another process")
            if (run["lease_owner"] and run["lease_expires_at"] is not None
                    and run["lease_expires_at"] <= now):
                active_ms = self._active_reserved_ms(
                    connection,
                    run_id,
                    run["lease_generation"],
                    run["lease_started_at"],
                    run["lease_expires_at"],
                    now,
                    run["invocation_reserved_ms"],
                )
                connection.execute(
                    """UPDATE corpus_attempts
                       SET status='abandoned',settled_at=?
                       WHERE run_id=? AND lease_generation=?
                         AND status='reserved'""",
                    (now, run_id, run["lease_generation"]),
                )
                connection.execute(
                    """UPDATE corpus_runs
                       SET seconds_used_ms=seconds_used_ms+?,
                           invocation_reserved_ms=0
                       WHERE run_id=?""",
                    (active_ms, run_id),
                )
                run = connection.execute(
                    "SELECT * FROM corpus_runs WHERE run_id=?",
                    (run_id,)).fetchone()
            manifest = self._manifest_from_row(run)
            self._bound_queries(connection, run_id, manifest)
            attempts_used = self._request_usage(connection, run_id)
            request_remaining = max(
                0, manifest.limits.requests - attempts_used)
            invocation_requests = min(
                request_remaining,
                manifest.limits.requests if max_requests is None
                else max(0, max_requests),
            )
            time_remaining_ms = max(
                0,
                int(manifest.limits.seconds * 1000)
                - run["seconds_used_ms"],
            )
            invocation_time_ms = min(
                120_000,
                time_remaining_ms,
                time_remaining_ms if max_seconds is None
                else max(0, int(max_seconds * 1000)),
            )
            entities_used = connection.execute(
                "SELECT COUNT(*) AS count FROM corpus_entities "
                "WHERE run_id=? AND admission_state='admitted'",
                (run_id,)).fetchone()["count"]
            unique_remaining = max(
                0, manifest.limits.unique_records - entities_used)
            invocation_new_records = min(
                unique_remaining,
                unique_remaining if max_new_records is None
                else max(0, max_new_records),
            )
            if ((invocation_requests == 0 or invocation_time_ms == 0)
                    and not replayable):
                connection.execute(
                    """UPDATE corpus_runs
                       SET state='paused_budget',revision=revision+1,
                           updated_at=? WHERE run_id=?""",
                    (now, run_id),
                )
                connection.commit()
                raise CorpusPaused("corpus invocation budget is exhausted")
            generation = run["lease_generation"] + 1
            expires_at = now + lease_seconds
            connection.execute(
                """UPDATE corpus_runs
                   SET state=CASE WHEN ? THEN state ELSE 'running' END,
                       revision=revision+1,
                       lease_owner=?,lease_generation=?,lease_expires_at=?,
                       lease_started_at=?,invocation_reserved_ms=?,
                       last_stop_json=CASE WHEN ? THEN last_stop_json
                                          ELSE NULL END,
                       updated_at=?
                   WHERE run_id=?""",
                (
                    replay_only, owner, generation, expires_at, now,
                    invocation_time_ms, replay_only, now, run_id,
                ),
            )
            connection.commit()
        return {
            "run_id": run_id,
            "owner": owner,
            "generation": generation,
            "expires_at": expires_at,
            "invocation_id": f"{owner}:{generation}",
            "start_revision": run["revision"] + 1,
            "time_started_monotonic": time.monotonic(),
            "request_limit": invocation_requests,
            "time_limit_ms": invocation_time_ms,
            "new_record_limit": invocation_new_records,
            "replay_only": replay_only,
        }

    def _assert_lease(
        self,
        connection,
        run_id,
        owner,
        generation,
        *,
        states=None,
        allow_any_state=False,
    ):
        row = connection.execute(
            """SELECT lease_owner,lease_generation,lease_expires_at,state
               FROM corpus_runs WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise XhhConfigError("corpus run not found")
        if (row["lease_owner"] != owner
                or row["lease_generation"] != generation):
            raise CorpusLocked("corpus lease was fenced by another process")
        if row["lease_expires_at"] is None or row["lease_expires_at"] <= time.time():
            raise CorpusLocked("corpus lease expired")
        allowed_states = states or {"running", "paused_budget", "blocked_human"}
        if not allow_any_state and row["state"] not in allowed_states:
            raise CorpusLocked(
                f"corpus run in state {row['state']!r} is not writable")

    def reserve_attempt(
        self,
        lease: dict,
        *,
        query_id: str | None,
        frame_key: str | None,
        role: str,
        protocol: str,
        invocation_limit: int,
    ) -> AttemptToken:
        now = time.time()
        run_id = lease["run_id"]
        owner = lease["owner"]
        generation = lease["generation"]
        with self._connect_rw() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(
                connection, run_id, owner, generation)
            pending = connection.execute(
                "SELECT 1 FROM corpus_deliveries "
                "WHERE run_id=? AND state='pending'", (run_id,)).fetchone()
            if pending:
                raise CorpusLocked(
                    "corpus must replay its pending delivery first")
            used = self._request_usage(
                connection, run_id,
                invocation_id=lease["invocation_id"])
            if used >= invocation_limit:
                raise CorpusPaused("invocation request budget is exhausted")
            manifest_row = connection.execute(
                "SELECT * FROM corpus_runs WHERE run_id=?",
                (run_id,)).fetchone()
            manifest = self._manifest_from_row(manifest_row)
            bound = self._bound_queries(connection, run_id, manifest)
            if query_id is not None and not any(
                    row["query_id"] == query_id for row in bound):
                raise CorpusConflict(
                    "corpus attempt references an unknown query",
                    code="corpus_query_binding_invalid",
                )
            total = self._request_usage(connection, run_id)
            if total >= manifest.limits.requests:
                raise CorpusPaused("manifest request budget is exhausted")
            attempt_id = (
                f"attempt-{time.time_ns()}-{generation}-{total + 1}")
            connection.execute(
                """INSERT INTO corpus_attempts(
                    attempt_id,run_id,query_id,frame_key,invocation_id,
                    lease_generation,role,protocol,reserved_ms,status,
                    started_at)
                VALUES (?,?,?,?,?,?,?,?,?,'reserved',?)""",
                (
                    attempt_id, run_id, query_id, frame_key,
                    lease["invocation_id"], generation, role, protocol,
                    lease["time_limit_ms"], now,
                ),
            )
            connection.commit()
        return AttemptToken(
            attempt_id=attempt_id,
            run_id=run_id,
            query_id=query_id,
            frame_key=frame_key,
            invocation_id=lease["invocation_id"],
            lease_generation=generation,
            time_reserved_ms=lease["time_limit_ms"],
        )

    def settle_attempt(
        self,
        lease: dict,
        token: AttemptToken,
        *,
        success: bool,
        error: dict | None = None,
    ) -> None:
        now = time.time()
        with self._connect_rw() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(
                connection, token.run_id, lease["owner"],
                token.lease_generation,
            )
            row = connection.execute(
                "SELECT status FROM corpus_attempts WHERE attempt_id=?",
                (token.attempt_id,),
            ).fetchone()
            if row is None:
                raise XhhConfigError("corpus attempt not found")
            if row["status"] != "reserved":
                if row["status"] in {"succeeded", "failed"}:
                    return
                raise XhhConfigError("corpus attempt is no longer writable")
            connection.execute(
                """UPDATE corpus_attempts
                   SET status=?,settled_at=?,error_json=?
                   WHERE attempt_id=?""",
                (
                    "succeeded" if success else "failed",
                    now,
                    canonical_json(error) if error is not None else None,
                    token.attempt_id,
                ),
            )
            connection.commit()

    def budget_view(self, lease: dict) -> dict:
        run_id = lease["run_id"]
        with self._connect_ro() as connection:
            run = connection.execute(
                "SELECT * FROM corpus_runs WHERE run_id=?",
                (run_id,)).fetchone()
            manifest = self._manifest_from_row(run)
            requests = self._request_usage(connection, run_id)
            unique = connection.execute(
                "SELECT COUNT(*) AS count FROM corpus_entities "
                "WHERE run_id=? AND admission_state='admitted'",
                (run_id,)).fetchone()["count"]
            invocation_new_records = connection.execute(
                "SELECT COUNT(*) AS count FROM corpus_entities "
                "WHERE run_id=? AND admission_state='admitted' "
                "AND admission_revision>=?",
                (run_id, lease["start_revision"]),
            ).fetchone()["count"]
        return {
            "requests": {"used": requests,
                         "limit": manifest.limits.requests},
            "seconds": {
                "used": round(
                    (run["seconds_used_ms"] + run["invocation_reserved_ms"])
                    / 1000, 3),
                "limit": manifest.limits.seconds,
            },
            "unique_records": {
                "used": unique,
                "limit": manifest.limits.unique_records,
            },
            "invocation": {
                "request_limit": lease["request_limit"],
                "seconds_limit": lease["time_limit_ms"] / 1000,
                "new_record_limit": lease["new_record_limit"],
                "new_records_used": invocation_new_records,
            },
        }

    def stop(
        self,
        lease: dict,
        decision: StopDecision,
    ) -> dict:
        now = time.time()
        with self._connect_rw() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(
                connection, lease["run_id"], lease["owner"],
                lease["generation"],
            )
            connection.execute(
                """UPDATE corpus_runs
                   SET state=?,revision=revision+1,last_stop_json=?,
                       updated_at=? WHERE run_id=?""",
                (
                    decision.next_state, canonical_json(decision.public()),
                    now, lease["run_id"],
                ),
            )
            connection.execute(
                """UPDATE corpus_queries
                   SET state=CASE
                         WHEN ?='paused_budget'
                           THEN CASE
                             WHEN pending_remainder_frame_key IS NOT NULL
                               THEN 'pending_remainder'
                             ELSE state
                           END
                         WHEN ?='paused_retryable'
                           THEN 'paused_retryable'
                         ELSE ?
                       END,
                       stop_reason=?,updated_at=?
                   WHERE run_id=?
                     AND query_id=COALESCE(?,(
                         SELECT query_id FROM corpus_queries
                         WHERE run_id=? AND state IN ('running','pending_remainder')
                         ORDER BY manifest_ordinal LIMIT 1
                     ))""",
                (
                    decision.next_state,
                    decision.next_state,
                    "failed" if decision.next_state == "failed"
                    else "blocked" if decision.next_state == "blocked_human"
                    else "pending_remainder",
                    decision.code, now, lease["run_id"],
                    decision.query_id, lease["run_id"],
                ),
            )
            connection.commit()
        return self.status(lease["run_id"])

    def finish(self, lease: dict) -> dict:
        now = time.time()
        with self._connect_rw() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(
                connection, lease["run_id"], lease["owner"],
                lease["generation"],
            )
            remaining = connection.execute(
                """SELECT COUNT(*) AS count FROM corpus_queries
                   WHERE run_id=? AND state NOT IN
                   ('complete','quota_reached')""",
                (lease["run_id"],),
            ).fetchone()["count"]
            state = "complete" if remaining == 0 else "paused_budget"
            connection.execute(
                """UPDATE corpus_runs
                   SET state=?,revision=revision+1,current_query_id=NULL,
                       updated_at=? WHERE run_id=?""",
                (state, now, lease["run_id"]),
            )
            connection.commit()
        return self.status(lease["run_id"])

    def release_lease(self, lease: dict) -> None:
        now = time.time()
        with self._connect_rw() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT lease_owner,lease_generation,lease_started_at,
                          invocation_reserved_ms,seconds_used_ms
                   FROM corpus_runs WHERE run_id=?""",
                (lease["run_id"],),
            ).fetchone()
            if row is None:
                return
            if (row["lease_owner"] != lease["owner"]
                    or row["lease_generation"] != lease["generation"]):
                return
            charged = self._active_reserved_ms(
                connection,
                lease["run_id"],
                lease["generation"],
                row["lease_started_at"],
                None,
                now,
                row["invocation_reserved_ms"],
            )
            connection.execute(
                """UPDATE corpus_runs
                   SET seconds_used_ms=seconds_used_ms+?,
                       invocation_reserved_ms=0,lease_owner=NULL,
                       lease_expires_at=NULL,lease_started_at=NULL,
                       updated_at=? WHERE run_id=?""",
                (charged, now, lease["run_id"]),
            )
            connection.commit()

    def find_frame(self, run_id: str, key: str) -> dict | None:
        with self._connect_ro() as connection:
            frame = connection.execute(
                "SELECT * FROM corpus_frames WHERE run_id=? AND frame_key=?",
                (run_id, key),
            ).fetchone()
            if frame is None:
                return None
            items = connection.execute(
                """SELECT ordinal,entity_key,eligibility,disposition,reason
                   FROM corpus_frame_items
                   WHERE run_id=? AND frame_key=? ORDER BY ordinal""",
                (run_id, key),
            )
            return {
                "frame_key": frame["frame_key"],
                "query_id": frame["query_id"],
                "current_position": json.loads(frame["current_position_json"]),
                "next_position": (
                    json.loads(frame["next_position_json"])
                    if frame["next_position_json"] else None
                ),
                "coverage": json.loads(frame["coverage_json"]),
                "evidence": json.loads(frame["evidence_json"]),
                "items": [dict(row) for row in items],
            }

    @staticmethod
    def _page_result_value(page: PageResult) -> dict:
        CorpusStore._validate_page_ordinals(page.items)
        return {
            "items": [asdict(item) for item in page.items],
            "coverage": page.coverage,
            "evidence": [asdict(item) for item in page.evidence],
            "current_position": page.current_position.canonical(),
            "next_position": (
                page.next_position.canonical()
                if page.next_position is not None else None
            ),
        }

    @staticmethod
    def _validate_page_ordinals(items):
        ordinals = [item.ordinal for item in items]
        if (
            any(type(ordinal) is not int or ordinal < 0 for ordinal in ordinals)
            or ordinals != list(range(len(ordinals)))
        ):
            raise CorpusConflict(
                "corpus page ordinals must be contiguous from zero",
                code="corpus_page_stage_invalid",
            )

    @staticmethod
    def _page_result_from_value(value: dict) -> PageResult:
        invalid = CorpusConflict(
            "corpus page stage is invalid",
            code="corpus_page_stage_invalid",
        )
        if not isinstance(value, dict):
            raise invalid
        try:
            raw_items = value["items"]
            raw_evidence = value["evidence"]
            if not isinstance(raw_items, list) or not isinstance(
                    raw_evidence, list):
                raise ValueError
            items = []
            for raw in raw_items:
                if (
                    not isinstance(raw, dict)
                    or type(raw["ordinal"]) is not int
                    or raw["ordinal"] < 0
                    or (raw["content"] is not None
                        and not isinstance(raw["content"], dict))
                    or (raw["entity_key"] is not None
                        and not isinstance(raw["entity_key"], str))
                    or not isinstance(raw["digest"], str)
                    or type(raw["eligible"]) is not bool
                    or (raw.get("reason") is not None
                        and not isinstance(raw.get("reason"), str))
                ):
                    raise ValueError
                items.append(ProviderItem(
                    ordinal=raw["ordinal"],
                    content=raw["content"],
                    entity_key=raw["entity_key"],
                    digest=raw["digest"],
                    eligible=raw["eligible"],
                    reason=raw.get("reason"),
                ))
            CorpusStore._validate_page_ordinals(items)
            evidence = []
            for raw in raw_evidence:
                if (
                    not isinstance(raw, dict)
                    or not isinstance(raw["route"], str)
                    or not isinstance(raw["protocol"], str)
                    or isinstance(raw["observed_at"], bool)
                    or not isinstance(raw["observed_at"], (int, float))
                    or not isinstance(raw["configured_uid"], str)
                    or type(raw["identity_verified"]) is not bool
                    or (raw.get("session_revision") is not None
                        and not isinstance(raw.get("session_revision"), str))
                ):
                    raise ValueError
                evidence.append(PageEvidence(
                    route=raw["route"],
                    protocol=raw["protocol"],
                    observed_at=float(raw["observed_at"]),
                    configured_uid=raw["configured_uid"],
                    identity_verified=raw["identity_verified"],
                    session_revision=raw.get("session_revision"),
                ))
            coverage_value = value["coverage"]
            if not isinstance(coverage_value, dict):
                raise ValueError
            current = value["current_position"]
            if (not isinstance(current, dict)
                    or not isinstance(current.get("mode"), str)
                    or "value" not in current):
                raise ValueError
            next_value = value["next_position"]
            if next_value is not None and (
                not isinstance(next_value, dict)
                or not isinstance(next_value.get("mode"), str)
                or "value" not in next_value
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise invalid from None
        return PageResult(
            items=tuple(items),
            coverage=coverage_value,
            evidence=tuple(evidence),
            current_position=Position(current["mode"], current["value"]),
            next_position=(
                Position(next_value["mode"], next_value["value"])
                if next_value is not None else None
            ),
        )

    def stage_page(
        self,
        lease: dict,
        token: AttemptToken,
        page: PageResult,
    ) -> None:
        now = time.time()
        run_id = lease["run_id"]
        if token.run_id != run_id or token.frame_key is None:
            raise CorpusConflict(
                "corpus page stage does not match its run",
                code="corpus_page_stage_invalid",
            )
        with self._connect_rw() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(
                connection, run_id, lease["owner"], lease["generation"],
                allow_any_state=True,
            )
            attempt = connection.execute(
                """SELECT run_id,query_id,frame_key,status FROM corpus_attempts
                   WHERE attempt_id=?""", (token.attempt_id,)).fetchone()
            if (
                attempt is None
                or attempt["run_id"] != run_id
                or attempt["query_id"] != token.query_id
                or attempt["frame_key"] != token.frame_key
            ):
                raise CorpusConflict(
                    "corpus page stage has no matching attempt",
                    code="corpus_page_stage_invalid",
                )
            existing = connection.execute(
                """SELECT state,page_json,query_id,frame_key
                   FROM corpus_page_stages
                   WHERE run_id=? AND attempt_id=?""",
                (run_id, token.attempt_id),
            ).fetchone()
            if existing is not None:
                if (existing["query_id"] != token.query_id
                        or existing["frame_key"] != token.frame_key):
                    raise CorpusConflict(
                        "corpus page stage does not match its attempt",
                        code="corpus_page_stage_invalid",
                    )
                if existing["state"] == "committed":
                    frame = connection.execute(
                        """SELECT attempt_id,query_id FROM corpus_frames
                           WHERE run_id=? AND frame_key=?""",
                        (run_id, token.frame_key),
                    ).fetchone()
                    if (attempt["status"] != "succeeded" or frame is None
                            or frame["attempt_id"] != token.attempt_id
                            or frame["query_id"] != token.query_id):
                        raise CorpusConflict(
                            "committed corpus stage has no matching frame",
                            code="corpus_page_stage_invalid",
                        )
                    return
            self._assert_lease(
                connection, run_id, lease["owner"], lease["generation"],
            )
            page_json = canonical_json(self._page_result_value(page))
            if existing is not None:
                if existing["page_json"] != page_json:
                    raise CorpusConflict(
                        "corpus page stage was replaced",
                        code="corpus_page_stage_invalid",
                    )
                connection.commit()
                return
            if attempt["status"] not in {"reserved", "succeeded"}:
                raise CorpusConflict(
                    "corpus page attempt cannot be staged",
                    code="corpus_page_stage_invalid",
                )
            run = connection.execute(
                "SELECT * FROM corpus_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            manifest = self._manifest_from_row(run)
            self._bound_queries(connection, run_id, manifest)
            connection.execute(
                """UPDATE corpus_attempts SET status='succeeded',settled_at=?
                   WHERE attempt_id=?""",
                (now, token.attempt_id),
            )
            connection.execute(
                """INSERT INTO corpus_page_stages(
                    run_id,attempt_id,query_id,frame_key,state,page_json,
                    staged_at)
                   VALUES (?,?,?,?,'pending',?,?)""",
                (
                    run_id, token.attempt_id, token.query_id, token.frame_key,
                    page_json, now,
                ),
            )
            connection.commit()

    def next_staged_token(self, lease: dict) -> AttemptToken | None:
        with self._connect_rw() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(
                connection, lease["run_id"], lease["owner"],
                lease["generation"], allow_any_state=True,
            )
            row = connection.execute(
                """SELECT stage.attempt_id,stage.query_id,stage.frame_key,
                          attempt.invocation_id,attempt.lease_generation,
                          attempt.reserved_ms
                   FROM corpus_page_stages stage
                   JOIN corpus_attempts attempt
                     ON attempt.attempt_id=stage.attempt_id
                   WHERE stage.run_id=? AND stage.state='pending'
                   ORDER BY stage.staged_at,stage.attempt_id LIMIT 1""",
                (lease["run_id"],),
            ).fetchone()
            connection.commit()
        if row is None:
            return None
        return AttemptToken(
            attempt_id=row["attempt_id"],
            run_id=lease["run_id"],
            query_id=row["query_id"],
            frame_key=row["frame_key"],
            invocation_id=row["invocation_id"],
            lease_generation=row["lease_generation"],
            time_reserved_ms=row["reserved_ms"],
        )

    def commit_page(
        self,
        lease: dict,
        query_id: str | None = None,
        token: AttemptToken | None = None,
        page: PageResult | None = None,
    ) -> dict:
        if token is None:
            raise XhhConfigError("corpus page attempt is required")
        query_id = query_id or token.query_id
        if query_id is None:
            raise CorpusConflict(
                "corpus page stage has no query",
                code="corpus_page_stage_invalid",
            )
        if token.run_id != lease["run_id"] or token.query_id != query_id:
            raise CorpusConflict(
                "corpus page attempt does not match its run and query",
                code="corpus_page_stage_invalid",
            )
        if page is not None:
            self.stage_page(lease, token, page)
        now = time.time()
        run_id = lease["run_id"]
        key = token.frame_key
        if key is None:
            raise XhhConfigError("corpus page attempt has no frame key")
        with self._connect_rw() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(
                connection, run_id, lease["owner"], lease["generation"],
                allow_any_state=True,
            )
            attempt = connection.execute(
                "SELECT run_id,status,frame_key,query_id FROM corpus_attempts "
                "WHERE attempt_id=?", (token.attempt_id,)).fetchone()
            if (attempt is None or attempt["status"] != "succeeded"
                    or attempt["run_id"] != run_id
                    or attempt["frame_key"] != key
                    or attempt["query_id"] != query_id):
                raise XhhConfigError(
                    "corpus page attempt is not committable")
            stage = connection.execute(
                """SELECT state,page_json,query_id,frame_key
                   FROM corpus_page_stages
                   WHERE run_id=? AND attempt_id=?""",
                (run_id, token.attempt_id),
            ).fetchone()
            if (
                stage is None
                or stage["query_id"] != query_id
                or stage["frame_key"] != key
            ):
                raise CorpusConflict(
                    "corpus page stage is not committable",
                    code="corpus_page_stage_invalid",
                )
            existing = connection.execute(
                """SELECT attempt_id,query_id FROM corpus_frames
                   WHERE run_id=? AND frame_key=?""",
                (run_id, key),
            ).fetchone()
            if existing is not None:
                if (existing["attempt_id"] != token.attempt_id
                        or existing["query_id"] != query_id):
                    raise CorpusConflict(
                        "corpus frame does not match its page attempt",
                        code="corpus_page_stage_invalid",
                    )
                connection.execute(
                    """UPDATE corpus_page_stages
                       SET state='committed',page_json='',
                           committed_at=COALESCE(committed_at,?)
                       WHERE run_id=? AND attempt_id=?""",
                    (now, run_id, token.attempt_id),
                )
                connection.commit()
                return {"replayed_frame": True, "frame_key": key}
            if stage["state"] != "pending":
                raise CorpusConflict(
                    "committed corpus stage has no matching frame",
                    code="corpus_page_stage_invalid",
                )
            page = self._page_result_from_value(json.loads(stage["page_json"]))
            query = connection.execute(
                "SELECT * FROM corpus_queries "
                "WHERE run_id=? AND query_id=?",
                (run_id, query_id),
            ).fetchone()
            run = connection.execute(
                "SELECT * FROM corpus_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if query is None:
                raise XhhConfigError("corpus query not found")
            manifest = self._manifest_from_row(run)
            bound = self._bound_queries(connection, run_id, manifest)
            spec = next(
                item for item in manifest.queries
                if item.query_id == query_id)
            pages_used = connection.execute(
                "SELECT COUNT(*) AS count FROM corpus_frames "
                "WHERE run_id=? AND query_id=?",
                (run_id, query_id),
            ).fetchone()["count"]
            if pages_used >= spec.limits.pages:
                raise CorpusPaused("query page budget is exhausted")
            revision = run["revision"] + 1
            global_used = connection.execute(
                "SELECT COUNT(*) AS count FROM corpus_entities "
                "WHERE run_id=? AND admission_state='admitted'",
                (run_id,),
            ).fetchone()["count"]
            query_used = connection.execute(
                "SELECT COUNT(*) AS count FROM corpus_entities "
                "WHERE run_id=? AND admission_state='admitted' "
                "AND admitting_query_id=?", (run_id, query_id),
            ).fetchone()["count"]
            invocation_used = connection.execute(
                "SELECT COUNT(*) AS count FROM corpus_entities "
                "WHERE run_id=? AND admission_state='admitted' "
                "AND admission_revision>=?",
                (run_id, lease["start_revision"]),
            ).fetchone()["count"]
            evidence = [asdict(item) for item in page.evidence]
            current_position = page.current_position.canonical()
            next_position = (
                page.next_position.canonical()
                if page.next_position is not None else None
            )
            connection.execute(
                """INSERT INTO corpus_frames(
                    run_id,frame_key,query_id,attempt_id,current_position_json,
                    next_position_json,provider_page_size,coverage_json,
                    evidence_json,observed_at,commit_revision)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, key, query_id, token.attempt_id,
                    canonical_json(current_position),
                    canonical_json(next_position) if next_position else None,
                    len(page.items), canonical_json(page.coverage),
                    canonical_json(evidence),
                    max((item["observed_at"] for item in evidence), default=now),
                    revision,
                ),
            )
            new_keys, updated_keys, membership_refs = [], [], []
            membership_seen = set()
            deferred = invalid = excluded = 0
            query_left = spec.limits.new_records - query_used
            global_left = manifest.limits.unique_records - global_used
            invocation_left = lease["new_record_limit"] - invocation_used
            for item in page.items:
                disposition = "invalid"
                reason = item.reason
                if item.content is None or item.entity_key is None:
                    invalid += 1
                    reason = "malformed_record"
                elif not item.eligible:
                    self._store_entity_content(
                        connection, run_id, item, key)
                    disposition = "invalid"
                    reason = "filter_excluded"
                    excluded += 1
                else:
                    current = connection.execute(
                        "SELECT * FROM corpus_entities "
                        "WHERE run_id=? AND entity_key=?",
                        (run_id, item.entity_key),
                    ).fetchone()
                    admitted = (
                        current is not None
                        and current["admission_state"] == "admitted"
                    )
                    can_admit = min(
                        query_left, global_left, invocation_left) > 0
                    if current is None:
                        connection.execute(
                            """INSERT INTO corpus_entities(
                                run_id,entity_key,kind,content_json,content_hash,
                                admission_state,first_frame_key,last_frame_key)
                            VALUES (?,?,?,?,?,'staged',?,?)""",
                            (
                                run_id, item.entity_key,
                                item.entity_key.split(":", 1)[0],
                                canonical_json(item.content),
                                observation_hash(item.content), key, key,
                            ),
                        )
                        current = connection.execute(
                            "SELECT * FROM corpus_entities "
                            "WHERE run_id=? AND entity_key=?",
                            (run_id, item.entity_key),
                        ).fetchone()
                    else:
                        merged = merge_content(
                            json.loads(current["content_json"]),
                            item.content,
                        )
                        content_json = canonical_json(merged)
                        connection.execute(
                            """UPDATE corpus_entities
                               SET content_json=?,content_hash=?,last_frame_key=?
                               WHERE run_id=? AND entity_key=?""",
                            (
                                content_json, observation_hash(merged), key,
                                run_id, item.entity_key,
                            ),
                        )
                    if admitted:
                        disposition = "known"
                        if (
                            item.entity_key not in new_keys
                            and item.entity_key not in updated_keys
                        ):
                            updated_keys.append(item.entity_key)
                    elif can_admit:
                        connection.execute(
                            """UPDATE corpus_entities
                               SET admission_state='admitted',
                                   admitting_query_id=?,admission_revision=?
                               WHERE run_id=? AND entity_key=?""",
                            (
                                query_id, revision, run_id, item.entity_key,
                            ),
                        )
                        if item.entity_key not in new_keys:
                            new_keys.append(item.entity_key)
                        query_left -= 1
                        global_left -= 1
                        invocation_left -= 1
                        disposition = "admitted"
                    else:
                        disposition = "deferred"
                        deferred += 1
                        reason = (
                            "query_new_record_budget"
                            if query_left <= 0 else
                            "manifest_unique_record_budget"
                            if global_left <= 0 else
                            "invocation_new_record_budget"
                        )
                if disposition in {"admitted", "known"}:
                    self._upsert_membership(
                        connection, run_id, query_id, item.entity_key,
                        key, revision,
                    )
                    membership_ref = [query_id, item.entity_key]
                    if tuple(membership_ref) not in membership_seen:
                        membership_seen.add(tuple(membership_ref))
                        membership_refs.append(membership_ref)
                connection.execute(
                    """INSERT INTO corpus_frame_items(
                        run_id,frame_key,ordinal,entity_key,item_digest,
                        eligibility,disposition,reason)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        run_id, key, item.ordinal, item.entity_key,
                        item.digest, "eligible" if item.eligible else "ineligible",
                        disposition, reason,
                    ),
                )
            pages_used += 1
            if deferred:
                query_state = (
                    "quota_reached"
                    if query_left <= 0
                    and global_left > 0
                    and invocation_left > 0
                    else "pending_remainder"
                )
            elif page.coverage.get("completion") == "complete":
                query_state = "complete"
            elif next_position is None:
                query_state = "failed"
            elif (pages_used >= spec.limits.pages
                  or query_used + len(new_keys) >= spec.limits.new_records):
                query_state = "quota_reached"
            else:
                query_state = "running"
            query_remainder = (
                key if deferred and query_state == "pending_remainder"
                else None
            )
            connection.execute(
                """UPDATE corpus_queries
                   SET state=?,current_position_json=?,next_position_json=?,
                       pending_remainder_frame_key=?,updated_at=?
                   WHERE run_id=? AND query_id=?""",
                (
                    query_state, canonical_json(current_position),
                    canonical_json(next_position) if next_position else None,
                    query_remainder, now, run_id, query_id,
                ),
            )
            if deferred and query_state == "pending_remainder":
                run_state = "paused_budget"
                stop = StopDecision(
                    "paused_budget",
                    "new_record_budget",
                    "corpus paused before admitting the page remainder",
                    True,
                    query_id=query_id,
                )
            elif query_state == "failed":
                run_state = "failed"
                stop = StopDecision(
                    "failed",
                    "continuation_missing",
                    "platform page has no safe continuation",
                    False,
                    query_id=query_id,
                )
            else:
                remaining = connection.execute(
                    """SELECT COUNT(*) AS count FROM corpus_queries
                       WHERE run_id=? AND query_id<>?
                         AND state NOT IN ('complete','quota_reached')""",
                    (run_id, query_id),
                ).fetchone()["count"]
                run_state = (
                    "running" if remaining or query_state == "running"
                    else "complete"
                )
                stop = None
            connection.execute(
                """UPDATE corpus_runs
                   SET state=?,revision=?,current_query_id=?,
                       last_stop_json=?,updated_at=? WHERE run_id=?""",
                (
                    run_state, revision,
                    query_id if run_state != "complete" else None,
                    canonical_json(stop.public()) if stop else None,
                    now, run_id,
                ),
            )
            delivery = self._insert_delivery(
                connection,
                run_id,
                revision,
                [key],
                new_keys,
                updated_keys,
                membership_refs,
                {
                    "provider": len(page.items),
                    "new": len(new_keys),
                    "updated": len(updated_keys),
                    "deferred": deferred,
                    "invalid": invalid,
                    "excluded": excluded,
                },
                {"run_state": run_state, "query_state": query_state},
                stop.public() if stop else None,
            )
            connection.execute(
                """UPDATE corpus_page_stages
                   SET state='committed',page_json='',committed_at=?
                   WHERE run_id=? AND attempt_id=? AND state='pending'""",
                (now, run_id, token.attempt_id),
            )
            connection.commit()
        return delivery

    def consume_remainder(
        self,
        lease: dict,
        query_id: str,
    ) -> dict | None:
        now = time.time()
        run_id = lease["run_id"]
        with self._connect_rw() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(
                connection, run_id, lease["owner"], lease["generation"])
            query = connection.execute(
                "SELECT * FROM corpus_queries "
                "WHERE run_id=? AND query_id=?",
                (run_id, query_id),
            ).fetchone()
            if (query is None or not query["pending_remainder_frame_key"]):
                connection.rollback()
                return None
            frame_key = query["pending_remainder_frame_key"]
            run = connection.execute(
                "SELECT * FROM corpus_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            revision = run["revision"] + 1
            manifest = self._manifest_from_row(run)
            self._bound_queries(connection, run_id, manifest)
            spec = next(
                item for item in manifest.queries
                if item.query_id == query_id)
            global_used = connection.execute(
                "SELECT COUNT(*) AS count FROM corpus_entities "
                "WHERE run_id=? AND admission_state='admitted'",
                (run_id,),
            ).fetchone()["count"]
            query_used = connection.execute(
                "SELECT COUNT(*) AS count FROM corpus_entities "
                "WHERE run_id=? AND admission_state='admitted' "
                "AND admitting_query_id=?", (run_id, query_id),
            ).fetchone()["count"]
            invocation_used = connection.execute(
                "SELECT COUNT(*) AS count FROM corpus_entities "
                "WHERE run_id=? AND admission_state='admitted' "
                "AND admission_revision>=?",
                (run_id, lease["start_revision"]),
            ).fetchone()["count"]
            query_left = spec.limits.new_records - query_used
            global_left = manifest.limits.unique_records - global_used
            invocation_left = lease["new_record_limit"] - invocation_used
            rows = list(connection.execute(
                """SELECT item.ordinal,item.entity_key,item.reason,
                          entity.admission_state
                   FROM corpus_frame_items item
                   JOIN corpus_entities entity
                     ON entity.run_id=item.run_id
                    AND entity.entity_key=item.entity_key
                   WHERE item.run_id=? AND item.frame_key=?
                     AND item.disposition='deferred'
                   ORDER BY item.ordinal""",
                (run_id, frame_key),
            ))
            new_keys, updated_keys, membership_refs = [], [], []
            membership_seen = set()
            consumed = 0
            blocking = None
            for row in rows:
                current = connection.execute(
                    """SELECT admission_state FROM corpus_entities
                       WHERE run_id=? AND entity_key=?""",
                    (run_id, row["entity_key"]),
                ).fetchone()
                if current is None:
                    raise CorpusConflict(
                        "corpus remainder references a missing entity",
                        code="corpus_delivery_invalid",
                    )
                admitted = current["admission_state"] == "admitted"
                can_admit = min(query_left, global_left, invocation_left) > 0
                if admitted:
                    disposition = "known"
                elif can_admit:
                    connection.execute(
                        """UPDATE corpus_entities
                           SET admission_state='admitted',
                               admitting_query_id=?,admission_revision=?
                           WHERE run_id=? AND entity_key=?""",
                        (
                            query_id, revision, run_id, row["entity_key"],
                        ),
                    )
                    query_left -= 1
                    global_left -= 1
                    invocation_left -= 1
                    new_keys.append(row["entity_key"])
                    disposition = "admitted"
                else:
                    blocking = (
                        "query_new_record_budget"
                        if query_left <= 0 else
                        "manifest_unique_record_budget"
                        if global_left <= 0 else
                        "invocation_new_record_budget"
                    )
                    break
                self._upsert_membership(
                    connection, run_id, query_id, row["entity_key"],
                    frame_key, revision,
                )
                membership_ref = [query_id, row["entity_key"]]
                if tuple(membership_ref) not in membership_seen:
                    membership_seen.add(tuple(membership_ref))
                    membership_refs.append(membership_ref)
                if (admitted and row["entity_key"] not in new_keys
                        and row["entity_key"] not in updated_keys):
                    updated_keys.append(row["entity_key"])
                connection.execute(
                    """UPDATE corpus_frame_items
                       SET disposition=?,reason=NULL
                       WHERE run_id=? AND frame_key=? AND ordinal=?""",
                    (
                        disposition, run_id, frame_key, row["ordinal"],
                    ),
                )
                consumed += 1
            remaining = len(rows) - consumed
            if remaining:
                query_state = (
                    "quota_reached"
                    if blocking == "query_new_record_budget"
                    else "pending_remainder"
                )
                if query_state == "quota_reached":
                    pending_queries = connection.execute(
                        """SELECT COUNT(*) AS count FROM corpus_queries
                           WHERE run_id=? AND query_id<>?
                             AND state NOT IN ('complete','quota_reached')""",
                        (run_id, query_id),
                    ).fetchone()["count"]
                    run_state = "running" if pending_queries else "complete"
                    stop = None
                else:
                    run_state = "paused_budget"
                    stop = StopDecision(
                        "paused_budget",
                        blocking,
                        "corpus paused before admitting the page remainder",
                        True,
                        query_id=query_id,
                    )
            else:
                next_position = (
                    json.loads(query["next_position_json"])
                    if query["next_position_json"] else None
                )
                pages_used = connection.execute(
                    "SELECT COUNT(*) AS count FROM corpus_frames "
                    "WHERE run_id=? AND query_id=?",
                    (run_id, query_id),
                ).fetchone()["count"]
                query_state = (
                    "complete" if next_position is None
                    else "quota_reached"
                    if pages_used >= spec.limits.pages or query_left <= 0
                    else "running"
                )
                pending_queries = connection.execute(
                    """SELECT COUNT(*) AS count FROM corpus_queries
                       WHERE run_id=? AND query_id<>?
                         AND state NOT IN ('complete','quota_reached')""",
                    (run_id, query_id),
                ).fetchone()["count"]
                run_state = (
                    "running" if pending_queries or query_state == "running"
                    else "complete"
                )
                stop = None
            connection.execute(
                """UPDATE corpus_queries
                   SET state=?,pending_remainder_frame_key=?,updated_at=?
                   WHERE run_id=? AND query_id=?""",
                (
                    query_state,
                    (
                        frame_key
                        if remaining and query_state == "pending_remainder"
                        else None
                    ),
                    now, run_id, query_id,
                ),
            )
            connection.execute(
                """UPDATE corpus_runs
                   SET state=?,revision=?,current_query_id=?,
                       last_stop_json=?,updated_at=? WHERE run_id=?""",
                (
                    run_state, revision,
                    query_id if run_state != "complete" else None,
                    canonical_json(stop.public()) if stop else None,
                    now, run_id,
                ),
            )
            delivery = self._insert_delivery(
                connection,
                run_id,
                revision,
                [frame_key],
                new_keys,
                updated_keys,
                membership_refs,
                {
                    "provider": 0,
                    "new": len(new_keys),
                    "updated": len(updated_keys),
                    "deferred": remaining,
                    "invalid": 0,
                    "excluded": 0,
                },
                {"run_state": run_state, "query_state": query_state},
                stop.public() if stop else None,
            )
            connection.commit()
        return delivery

    @staticmethod
    def _store_entity_content(connection, run_id, item, frame_key):
        current = connection.execute(
            "SELECT content_json FROM corpus_entities "
            "WHERE run_id=? AND entity_key=?",
            (run_id, item.entity_key),
        ).fetchone()
        if current is None:
            connection.execute(
                """INSERT INTO corpus_entities(
                    run_id,entity_key,kind,content_json,content_hash,
                    admission_state,first_frame_key,last_frame_key)
                VALUES (?,?,?,?,?,'staged',?,?)""",
                (
                    run_id, item.entity_key,
                    item.entity_key.split(":", 1)[0],
                    canonical_json(item.content),
                    observation_hash(item.content), frame_key, frame_key,
                ),
            )
            return
        merged = merge_content(json.loads(current["content_json"]), item.content)
        connection.execute(
            """UPDATE corpus_entities
               SET content_json=?,content_hash=?,last_frame_key=?
               WHERE run_id=? AND entity_key=?""",
            (
                canonical_json(merged), observation_hash(merged), frame_key,
                run_id, item.entity_key,
            ),
        )

    @staticmethod
    def _upsert_membership(
        connection, run_id, query_id, entity_key, frame_key, revision,
    ):
        connection.execute(
            """INSERT INTO corpus_memberships(
                run_id,query_id,entity_key,first_frame_key,last_frame_key,
                first_revision,last_revision)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(run_id,query_id,entity_key) DO UPDATE SET
                last_frame_key=excluded.last_frame_key,
                last_revision=excluded.last_revision""",
            (
                run_id, query_id, entity_key, frame_key, frame_key,
                revision, revision,
            ),
        )

    @staticmethod
    def _insert_delivery(
        connection, run_id, revision, frame_keys, new_keys, updated_keys,
        membership_refs, counts, state, stop,
    ):
        delivery_id = f"{run_id}:{revision}:{time.time_ns()}"
        refs = {
            "frames": frame_keys,
            "entities": {
                "new": new_keys,
                "updated": updated_keys,
            },
            "memberships": membership_refs,
        }
        connection.execute(
            """INSERT INTO corpus_deliveries(
                run_id,revision,delivery_id,state,frame_refs_json,
                entity_refs_json,membership_refs_json,counts_json,
                state_json,stop_json,created_at)
            VALUES (?,?,?,'pending',?,?,?,?,?,?,?)""",
            (
                run_id, revision, delivery_id,
                canonical_json(refs["frames"]),
                canonical_json(refs["entities"]),
                canonical_json(refs["memberships"]),
                canonical_json(counts), canonical_json(state),
                canonical_json(stop) if stop else None,
                time.time(),
            ),
        )
        return {
            "delivery_id": delivery_id,
            "revision": revision,
            "frame_keys": frame_keys,
            "new_keys": new_keys,
            "updated_keys": updated_keys,
            "membership_refs": membership_refs,
            "counts": counts,
            "state": state,
            "stop": stop,
        }

    def pending_delivery(self, run_id: str) -> dict | None:
        with self._connect_ro() as connection:
            row = connection.execute(
                "SELECT * FROM corpus_deliveries "
                "WHERE run_id=? AND state='pending'", (run_id,),
            ).fetchone()
            if row is None:
                return None
            invalid = CorpusConflict(
                "corpus delivery does not match durable state",
                code="corpus_delivery_invalid",
            )
            try:
                frames = json.loads(row["frame_refs_json"])
                entities = json.loads(row["entity_refs_json"])
                memberships = json.loads(row["membership_refs_json"])
                counts = json.loads(row["counts_json"])
                recorded_state = json.loads(row["state_json"])
                recorded_stop = None
                if row["stop_json"] is not None:
                    recorded_stop = json.loads(row["stop_json"])
                    if not isinstance(recorded_stop, dict):
                        raise invalid
            except (TypeError, ValueError):
                raise invalid from None
            if (
                not isinstance(frames, list)
                or not frames
                or not all(isinstance(key, str) for key in frames)
                or not isinstance(entities, dict)
                or set(entities) != {"new", "updated"}
                or not all(
                    isinstance(entities[key], list)
                    and all(isinstance(item, str) for item in entities[key])
                    for key in ("new", "updated")
                )
                or not isinstance(memberships, list)
                or not all(
                    isinstance(ref, list)
                    and len(ref) == 2
                    and all(isinstance(item, str) for item in ref)
                    for ref in memberships
                )
                or not isinstance(counts, dict)
                or set(counts) not in (
                    {"provider", "new", "updated", "deferred", "invalid"},
                    {"provider", "new", "updated", "deferred", "invalid",
                     "excluded"},
                )
                or not all(
                    type(value) is int and value >= 0
                    for value in counts.values()
                )
                or counts["new"] != len(entities["new"])
                or counts["updated"] != len(entities["updated"])
            ):
                raise invalid
            if (
                len(frames) != len(set(frames))
                or len(entities["new"]) != len(set(entities["new"]))
                or len(entities["updated"]) != len(set(entities["updated"]))
                or set(entities["new"]).intersection(entities["updated"])
                or len(memberships) != len({
                    tuple(ref) for ref in memberships
                })
            ):
                raise invalid
            pages = []
            durable_frames = []
            frame_query_ids = set()
            for frame_key in frames:
                frame = connection.execute(
                    "SELECT * FROM corpus_frames "
                    "WHERE run_id=? AND frame_key=?",
                    (run_id, frame_key),
                ).fetchone()
                if frame is None:
                    raise invalid
                durable_frames.append(frame)
                frame_query_ids.add(frame["query_id"])
                pages.append({
                    "frame_key": frame_key,
                    "query_id": frame["query_id"],
                    "position": json.loads(frame["current_position_json"]),
                    "coverage": json.loads(frame["coverage_json"]),
                })
            if len(frame_query_ids) != 1:
                raise invalid
            query_id = next(iter(frame_query_ids))
            run = connection.execute(
                "SELECT state,last_stop_json FROM corpus_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            query = connection.execute(
                """SELECT state,pending_remainder_frame_key
                   FROM corpus_queries WHERE run_id=? AND query_id=?""",
                (run_id, query_id),
            ).fetchone()
            if run is None or query is None:
                raise invalid
            try:
                durable_stop = None
                if run["last_stop_json"] is not None:
                    durable_stop = json.loads(run["last_stop_json"])
                    if not isinstance(durable_stop, dict):
                        raise invalid
            except (TypeError, ValueError):
                raise invalid from None
            if (
                recorded_state != {
                    "run_state": run["state"],
                    "query_state": query["state"],
                }
                or recorded_stop != durable_stop
            ):
                raise invalid
            revision = row["revision"]
            page_delivery = all(
                frame["commit_revision"] == revision
                for frame in durable_frames)
            if not page_delivery and not all(
                    frame["commit_revision"] < revision
                    for frame in durable_frames):
                raise invalid
            committed_frames = {
                frame["frame_key"] for frame in connection.execute(
                    "SELECT frame_key FROM corpus_frames "
                    "WHERE run_id=? AND commit_revision=?",
                    (run_id, revision),
                )
            }
            if committed_frames and committed_frames != set(frames):
                raise invalid
            touched_frames = {
                membership["last_frame_key"]
                for membership in connection.execute(
                    "SELECT last_frame_key FROM corpus_memberships "
                    "WHERE run_id=? AND last_revision=?",
                    (run_id, revision),
                )
            }
            if touched_frames and touched_frames != set(frames):
                raise invalid
            frame_rows = list(connection.execute(
                f"""SELECT frame_key,ordinal,entity_key,eligibility,
                           disposition,reason
                    FROM corpus_frame_items
                    WHERE run_id=? AND frame_key IN
                      ({','.join('?' for _ in frames)})""",
                (run_id, *frames),
            ))
            if len(frame_rows) != sum(
                    frame["provider_page_size"] for frame in durable_frames):
                raise invalid
            ordinals = {}
            for item in frame_rows:
                ordinals.setdefault(item["frame_key"], []).append(
                    item["ordinal"])
            if any(
                sorted(ordinals.get(frame["frame_key"], []))
                != list(range(frame["provider_page_size"]))
                for frame in durable_frames
            ):
                raise invalid
            expected_counts = {
                "provider": len(frame_rows) if page_delivery else 0,
                "new": 0,
                "updated": 0,
                "deferred": 0,
                "invalid": 0,
                "excluded": 0,
            }
            resolved = {}
            for item in frame_rows:
                if item["disposition"] == "deferred":
                    expected_counts["deferred"] += 1
                elif item["disposition"] == "invalid" and page_delivery:
                    bucket = (
                        "excluded" if item["reason"] == "filter_excluded"
                        else "invalid")
                    expected_counts[bucket] += 1
                elif item["disposition"] in {"admitted", "known"}:
                    if (item["entity_key"] is None
                            or item["eligibility"] != "eligible"):
                        raise invalid
                    resolved.setdefault(
                        item["entity_key"], set()).add(item["disposition"])
            remainder_key = query["pending_remainder_frame_key"]
            if expected_counts["deferred"]:
                if query["state"] == "pending_remainder":
                    if remainder_key is None or remainder_key not in frames:
                        raise invalid
                    if not any(
                        item["frame_key"] == remainder_key
                        and item["disposition"] == "deferred"
                        for item in frame_rows
                    ):
                        raise invalid
                elif query["state"] == "quota_reached":
                    if remainder_key is not None:
                        raise invalid
                else:
                    raise invalid
            elif remainder_key is not None:
                raise invalid
            expected_entities = {"new": set(), "updated": set()}
            contents = {}
            for entity_key, dispositions in resolved.items():
                entity = connection.execute(
                    """SELECT content_json,content_hash,admission_state,
                              admission_revision
                       FROM corpus_entities
                       WHERE run_id=? AND entity_key=?""",
                    (run_id, entity_key),
                ).fetchone()
                membership = connection.execute(
                    """SELECT last_frame_key,last_revision
                       FROM corpus_memberships
                       WHERE run_id=? AND query_id=? AND entity_key=?""",
                    (run_id, query_id, entity_key),
                ).fetchone()
                if (
                    entity is None
                    or entity["admission_state"] != "admitted"
                    or entity["admission_revision"] is None
                    or entity["admission_revision"] > revision
                    or membership is None
                    or membership["last_revision"] > revision
                    or (page_delivery
                        and membership["last_revision"] != revision)
                ):
                    raise invalid
                if membership["last_revision"] != revision:
                    continue
                bucket = (
                    "new" if entity["admission_revision"] == revision
                    else "updated")
                if (membership["last_frame_key"] not in frames
                        or (bucket == "new" and "admitted" not in dispositions)
                        or (bucket == "updated" and "known" not in dispositions)):
                    raise invalid
                expected_entities[bucket].add(entity_key)
                try:
                    content = json.loads(entity["content_json"])
                except (TypeError, ValueError):
                    raise invalid from None
                if (
                    not isinstance(content, dict)
                    or observation_hash(content) != entity["content_hash"]
                ):
                    raise invalid
                contents[entity_key] = content
            if any(set(entities[bucket]) != expected_entities[bucket]
                   for bucket in ("new", "updated")):
                raise invalid
            expected_memberships = {
                (query_id, entity_key)
                for bucket in expected_entities.values()
                for entity_key in bucket
            }
            if set(map(tuple, memberships)) != expected_memberships:
                raise invalid
            for bucket in ("new", "updated"):
                expected_counts[bucket] = len(expected_entities[bucket])
            counts.setdefault("excluded", expected_counts["excluded"])
            if counts != expected_counts:
                raise invalid
            loaded = {
                bucket: [contents[entity_key] for entity_key in entities[bucket]]
                for bucket in ("new", "updated")
            }
            remainders = [
                dict(row) for row in connection.execute(
                    """SELECT query_id,pending_remainder_frame_key,
                              (SELECT COUNT(*) FROM corpus_frame_items i
                               WHERE i.run_id=q.run_id
                                 AND i.frame_key=q.pending_remainder_frame_key
                                 AND i.disposition='deferred') AS count
                       FROM corpus_queries q
                       WHERE q.run_id=? AND q.pending_remainder_frame_key
                         IS NOT NULL
                         AND q.state NOT IN ('complete','quota_reached')
                         AND EXISTS (
                           SELECT 1 FROM corpus_frame_items item
                           WHERE item.run_id=q.run_id
                             AND item.frame_key=q.pending_remainder_frame_key
                             AND item.disposition='deferred'
                         )
                       ORDER BY q.manifest_ordinal""",
                    (run_id,),
                )
            ]
        return {
            "delivery_id": row["delivery_id"],
            "revision": row["revision"],
            "pages": pages,
            "new_items": loaded["new"],
            "updated_items": loaded["updated"],
            "memberships": memberships,
            "remainders": remainders,
            "counts": counts,
            "state": json.loads(row["state_json"]),
            "stop": recorded_stop,
        }

    def acknowledge_delivery(
        self,
        run_id: str,
        revision: int,
        *,
        lease=None,
    ) -> None:
        with self._connect_rw() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if lease is not None:
                self._assert_lease(
                    connection,
                    run_id,
                    lease["owner"],
                    lease["generation"],
                    allow_any_state=True,
                )
            pending = connection.execute(
                """SELECT 1 FROM corpus_deliveries
                   WHERE run_id=? AND revision=? AND state='pending'""",
                (run_id, revision),
            ).fetchone()
            if pending is None:
                connection.commit()
                return
            validated = self.pending_delivery(run_id)
            if validated is None or validated["revision"] != revision:
                raise CorpusConflict(
                    "corpus delivery does not match durable state",
                    code="corpus_delivery_invalid",
                )
            connection.execute(
                """UPDATE corpus_deliveries
                   SET state='acknowledged',acknowledged_at=?
                   WHERE run_id=? AND revision=? AND state='pending'""",
                (time.time(), run_id, revision),
            )
            connection.commit()


def _sanitized_query(query) -> dict:
    return {
        "query_id": query.query_id,
        "kind": query.kind,
        "target": query.target,
        "filters": query.filters,
        "limits": query.limits.canonical(),
        "sha256": query.sha256(),
    }
