from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from documents import enrich_observation
from xhh_sdk.exceptions import XhhConfigError

RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
IDENTITY = re.compile(r"[1-9][0-9]{0,29}\Z")
FILTER_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
SECRET_KEYS = frozenset({
    "authorization", "cookie", "cookies", "password", "pkey", "secret",
    "token", "user_pkey", "x_pkey", "risk_token", "signer_path",
})
CLI_ONLY_FIELDS = frozenset({
    "account", "config", "data_dir", "env", "protocol", "requests",
    "seconds", "pages", "snapshot", "since", "resume", "raw",
})
QUERY_KINDS = frozenset({"feed", "search", "hashtag_links"})
RUN_STATES = frozenset({
    "planned", "running", "paused_budget", "paused_retryable",
    "blocked_human", "complete", "failed",
})
QUERY_STATES = frozenset({
    "pending", "running", "pending_remainder", "paused_retryable",
    "quota_reached", "complete", "blocked", "failed",
})


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _reject_constant(value: str):
    raise ValueError(f"non-finite JSON number: {value}")


def _manifest_value(text: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        text, object_pairs_hook=pairs, parse_constant=_reject_constant)


def _object(value: Any, name: str, expected: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise XhhConfigError(f"{name} must be an object")
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing:
        raise XhhConfigError(
            f"{name} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise XhhConfigError(
            f"{name} has unknown fields: {', '.join(sorted(unknown))}")
    for key in value:
        normalized = str(key).casefold()
        if normalized in SECRET_KEYS:
            raise XhhConfigError(f"{name} cannot contain {key!r}")
    return value


def _positive_int(value: Any, name: str, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise XhhConfigError(f"{name} must be an integer")
    if not 1 <= value <= maximum:
        raise XhhConfigError(f"{name} must be within 1..{maximum}")
    return value


def _identity(value: Any, name: str) -> str:
    selected = str(value or "")
    if not IDENTITY.fullmatch(selected):
        raise XhhConfigError(f"{name} must be a positive decimal ID")
    return selected


def _name(value: Any, name: str) -> str:
    if not isinstance(value, str) or not RUN_ID.fullmatch(value):
        raise XhhConfigError(
            f"{name} must use 1-64 letters, digits, underscores or hyphens")
    return value


def _text(value: Any, name: str, maximum: int = 256) -> str:
    if (not isinstance(value, str) or not value.strip()
            or len(value) > maximum or any(ch in value for ch in "\x00\r\n")):
        raise XhhConfigError(f"{name} must be nonempty single-line text")
    return value


@dataclass(frozen=True)
class GlobalLimits:
    requests: int
    seconds: float
    unique_records: int

    def canonical(self) -> dict:
        return {
            "requests": self.requests,
            "seconds": self.seconds,
            "unique_records": self.unique_records,
        }


@dataclass(frozen=True)
class QueryLimits:
    pages: int
    new_records: int

    def canonical(self) -> dict:
        return {"pages": self.pages, "new_records": self.new_records}


@dataclass(frozen=True)
class ReaderPolicy:
    protocol: str
    page_size: int
    _identity_json: str | None = field(
        default=None, init=False, repr=False, compare=False)

    def canonical(self) -> dict:
        if self._identity_json is not None:
            return json.loads(self._identity_json)
        return {
            "protocol": self.protocol,
            "page_size": self.page_size,
        }

    def sha256(self) -> str:
        return sha256_value(self.canonical())


@dataclass(frozen=True)
class QuerySpec:
    query_id: str
    kind: str
    target: dict[str, Any]
    filters: dict[str, Any]
    limits: QueryLimits

    def canonical(self) -> dict:
        return {
            "query_id": self.query_id,
            "kind": self.kind,
            "target": self.target,
            "filters": self.filters,
            "limits": self.limits.canonical(),
        }

    def sha256(self) -> str:
        return sha256_value(self.canonical())


@dataclass(frozen=True)
class CorpusManifest:
    schema_version: int
    run_id: str
    account_alias: str
    account_uid: str
    reader: ReaderPolicy
    queries: tuple[QuerySpec, ...]
    limits: GlobalLimits
    _identity_json: str | None = field(
        default=None, init=False, repr=False, compare=False)

    def canonical(self) -> dict:
        if self._identity_json is not None:
            return json.loads(self._identity_json)
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "account": {
                "alias": self.account_alias,
                "uid": self.account_uid,
            },
            "reader": self.reader.canonical(),
            "queries": [query.canonical() for query in self.queries],
            "limits": self.limits.canonical(),
        }

    def sha256(self) -> str:
        return sha256_value(self.canonical())

    @classmethod
    def load(cls, path: Path | str) -> "CorpusManifest":
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
            value = _manifest_value(text)
        except (OSError, UnicodeError, ValueError) as error:
            raise XhhConfigError(f"corpus manifest is invalid: {error}") from None
        return cls.from_value(value)

    @classmethod
    def from_value(cls, value: Any) -> "CorpusManifest":
        return cls._parse_value(value, persisted=False)

    @classmethod
    def _from_persisted_json(
        cls, text: str, manifest_sha256: str, reader_hash: str,
    ) -> "CorpusManifest":
        try:
            value = _manifest_value(text)
            if (sha256_value(value) != manifest_sha256
                    or sha256_value(value["reader"]) != reader_hash):
                raise XhhConfigError("persisted corpus identity hash mismatch")
            manifest = cls._parse_value(value, persisted=True)
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise XhhConfigError(
                f"persisted corpus manifest is invalid: {error}") from None
        object.__setattr__(manifest, "_identity_json", canonical_json(value))
        object.__setattr__(
            manifest.reader, "_identity_json", canonical_json(value["reader"]))
        return manifest

    @classmethod
    def _parse_value(cls, value: Any, *, persisted: bool) -> "CorpusManifest":
        root = _object(
            value, "manifest",
            {"schema_version", "run_id", "account", "reader", "queries",
             "limits"},
        )
        if (type(root["schema_version"]) is not int
                or root["schema_version"] != 1):
            raise XhhConfigError("manifest schema_version must be 1")
        account = _object(root["account"], "account", {"alias", "uid"})
        alias = _text(account["alias"], "account alias", 64)
        uid = _identity(account["uid"], "account UID")
        reader_fields = {"protocol", "page_size"}
        if (persisted and isinstance(root["reader"], dict)
                and "order" in root["reader"]):
            reader_fields.add("order")
        reader_value = _object(root["reader"], "reader", reader_fields)
        protocol = reader_value["protocol"]
        if protocol not in {"app", "web"}:
            raise XhhConfigError("reader protocol must be app or web")
        page_size = _positive_int(
            reader_value["page_size"], "reader page_size", 100)
        if "order" in reader_value:
            order = reader_value["order"]
            if (not isinstance(order, list) or not 1 <= len(order) <= 4
                    or any(not isinstance(item, str) or item not in {
                        "native", "website", "html", "auto",
                    } for item in order)
                    or len(set(order)) != len(order)):
                raise XhhConfigError(
                    "reader order must contain unique reader names")
        limits_value = _object(
            root["limits"], "limits",
            {"requests", "seconds", "unique_records"},
        )
        seconds = limits_value["seconds"]
        maximum_seconds = 86400 if persisted else 120
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not 0 < seconds <= maximum_seconds):
            raise XhhConfigError(
                f"limits seconds must be within 0..{maximum_seconds}")
        queries_value = root["queries"]
        if not isinstance(queries_value, list) or not 1 <= len(queries_value) <= 100:
            raise XhhConfigError("queries must contain between 1 and 100 items")
        queries = tuple(_query(item) for item in queries_value)
        ids = [query.query_id for query in queries]
        if len(ids) != len(set(ids)):
            raise XhhConfigError("query IDs must be unique")
        return cls(
            schema_version=1,
            run_id=_name(root["run_id"], "run ID"),
            account_alias=alias,
            account_uid=uid,
            reader=ReaderPolicy(protocol, page_size),
            queries=queries,
            limits=GlobalLimits(
                _positive_int(limits_value["requests"], "limits requests"),
                float(seconds),
                _positive_int(
                    limits_value["unique_records"],
                    "limits unique_records",
                ),
            ),
        )


def _query(value: Any) -> QuerySpec:
    row = _object(
        value, "query",
        {"query_id", "kind", "target", "filters", "limits"},
    )
    kind = row["kind"]
    if kind not in QUERY_KINDS:
        raise XhhConfigError(f"unsupported corpus query kind: {kind!r}")
    target = row["target"]
    filters = row["filters"]
    if not isinstance(target, dict) or not isinstance(filters, dict):
        raise XhhConfigError("query target and filters must be objects")
    target_keys = {
        "feed": {"topic_id"},
        "search": {"query"},
        "hashtag_links": {"hashtag_id", "hashtag_name"},
    }[kind]
    target = dict(_object(target, f"{kind} target", target_keys))
    if kind == "feed":
        target["topic_id"] = _identity(target["topic_id"], "feed topic_id")
    elif kind == "search":
        target["query"] = _text(target["query"], "search query")
    else:
        target["hashtag_id"] = _identity(
            target["hashtag_id"], "hashtag ID")
        target["hashtag_name"] = _text(
            target["hashtag_name"], "hashtag name")
    allowed = {
        "feed": {
            "order", "time_range", "platform_filters", "content_type",
            "after", "min_comments", "sort",
        },
        "search": {
            "order", "time_range", "platform_filters", "community",
            "content_type", "after", "min_comments", "sort",
        },
        "hashtag_links": set(),
    }[kind]
    unknown = set(filters) - allowed
    if unknown:
        raise XhhConfigError(
            f"{kind} query has unsupported filters: "
            f"{', '.join(sorted(unknown))}")
    canonical_filters = {}
    for key, item in sorted(filters.items()):
        if key.casefold() in SECRET_KEYS or key in CLI_ONLY_FIELDS:
            raise XhhConfigError(f"query filter cannot contain {key!r}")
        if key == "platform_filters":
            if not isinstance(item, dict):
                raise XhhConfigError("platform_filters must be an object")
            canonical_filters[key] = {}
            for platform_key, platform_value in sorted(item.items()):
                if (not FILTER_KEY.fullmatch(str(platform_key))
                        or str(platform_key).casefold() in SECRET_KEYS):
                    raise XhhConfigError(
                        f"invalid platform filter key: {platform_key!r}")
                canonical_filters[key][str(platform_key)] = _text(
                    platform_value, f"platform filter {platform_key}")
        elif key in {"after", "min_comments"}:
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise XhhConfigError(f"{key} must be a nonnegative integer")
            canonical_filters[key] = item
        elif key == "content_type":
            if item not in {"all", "article", "dynamic"}:
                raise XhhConfigError("content_type is invalid")
            canonical_filters[key] = item
        elif key == "sort":
            if item not in {"platform", "created", "comments", "charges"}:
                raise XhhConfigError("sort is invalid")
            canonical_filters[key] = item
        else:
            canonical_filters[key] = _text(item, key)
    limits = _object(
        row["limits"], "query limits", {"pages", "new_records"})
    return QuerySpec(
        query_id=_name(row["query_id"], "query ID"),
        kind=kind,
        target=target,
        filters=canonical_filters,
        limits=QueryLimits(
            _positive_int(limits["pages"], "query pages"),
            _positive_int(limits["new_records"], "query new_records"),
        ),
    )


@dataclass(frozen=True)
class Position:
    mode: str
    value: int | str | None

    def canonical(self) -> dict:
        return {"mode": self.mode, "value": self.value}


def frame_key(query: QuerySpec, position: Position,
              reader: ReaderPolicy) -> str:
    return sha256_value({
        "query_id": query.query_id,
        "query_sha256": query.sha256(),
        "position": position.canonical(),
        "page_size": reader.page_size,
        "reader_sha256": reader.sha256(),
    })


@dataclass(frozen=True)
class EntityKey:
    kind: str
    identity: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.identity}"


def entity_key(kind: str, identity: Any, *, post_id: Any = None,
               root_id: Any = None) -> str:
    selected = _identity(identity, "entity identity")
    if kind == "comment":
        post = _identity(post_id, "comment post ID")
        root = _identity(root_id, "comment root ID")
        return f"comment:{post}:{root}:{selected}"
    if kind not in {"post", "community", "user", "game", "hashtag"}:
        raise XhhConfigError(f"unsupported entity kind: {kind}")
    return str(EntityKey(kind, selected))


@dataclass(frozen=True)
class ProviderItem:
    ordinal: int
    content: dict[str, Any] | None
    entity_key: str | None
    digest: str
    eligible: bool
    reason: str | None = None


@dataclass(frozen=True)
class PageEvidence:
    route: str
    protocol: str
    observed_at: float
    configured_uid: str
    identity_verified: bool
    session_revision: str | None


@dataclass(frozen=True)
class PageResult:
    items: tuple[ProviderItem, ...]
    coverage: dict[str, Any]
    evidence: tuple[PageEvidence, ...]
    current_position: Position
    next_position: Position | None


@dataclass(frozen=True)
class AttemptToken:
    attempt_id: str
    run_id: str
    query_id: str | None
    frame_key: str | None
    invocation_id: str
    lease_generation: int
    request_reserved: int = 1
    time_reserved_ms: int = 0


@dataclass(frozen=True)
class StopDecision:
    next_state: str
    code: str
    message: str
    retry_allowed: bool
    not_before: float | None = None
    fix: str | None = None
    query_id: str | None = None
    phase: str = "read"

    def public(self) -> dict:
        value = {
            "code": self.code,
            "message": self.message,
            "phase": self.phase,
            "retry_allowed": self.retry_allowed,
        }
        if self.not_before is not None:
            value["not_before"] = self.not_before
        if self.fix:
            value["fix"] = self.fix
        if self.query_id:
            value["query_id"] = self.query_id
        return value


def observation_hash(content: Mapping[str, Any]) -> str:
    comparable = {
        key: value for key, value in content.items()
        if key not in {"raw", "raw_pages", "raw_responses", "observed_at",
                       "source", "content_sha256"}
    }
    return sha256_value(comparable)


def merge_content(previous: Mapping[str, Any],
                  latest: Mapping[str, Any]) -> dict[str, Any]:
    return enrich_observation(dict(previous), dict(latest))
