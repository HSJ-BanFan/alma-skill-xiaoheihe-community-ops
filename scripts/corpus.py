from __future__ import annotations

import time
from dataclasses import dataclass

from corpus_model import (
    CorpusManifest,
    PageEvidence,
    PageResult,
    Position,
    ProviderItem,
    QuerySpec,
    StopDecision,
    frame_key,
)
from paging import Budget, ReadLimit
from corpus_store import CorpusPaused
from xhh_sdk.exceptions import (
    XhhAPIError,
    XhhAuthError,
    XhhCaptchaError,
    XhhConfigError,
    XhhError,
    XhhRateLimitError,
    XhhSignerError,
    XhhTransportError,
)


class CorpusBudget:
    def __init__(
        self,
        store,
        lease,
        *,
        query_id=None,
        frame_key=None,
        role="page",
        protocol="app",
    ):
        self.store = store
        self.lease = lease
        self.query_id = query_id
        self.frame_key = frame_key
        self.role = role
        self.protocol = protocol
        self.last_token = None
        self.deadline = (
            lease.get("time_started_monotonic", time.monotonic())
            + lease["time_limit_ms"] / 1000
        )

    def _remaining(self):
        return self.deadline - time.monotonic()

    def _attempt(self, protocol, execute):
        remaining = self._remaining()
        if remaining <= 0:
            raise ReadLimit("time_budget")
        try:
            token = self.store.reserve_attempt(
                self.lease,
                query_id=self.query_id,
                frame_key=self.frame_key,
                role=self.role,
                protocol=protocol,
                invocation_limit=self.lease["request_limit"],
            )
        except CorpusPaused:
            raise ReadLimit("request_budget") from None
        self.last_token = token
        remaining = self._remaining()
        if remaining <= 0:
            error = ReadLimit("time_budget")
            self.store.settle_attempt(
                self.lease, token, success=False, error=error.public())
            raise error
        local = Budget(
            requests=1,
            seconds=max(0.001, remaining),
            pages=1,
            items=100,
        )
        local.deadline = self.deadline
        try:
            value = execute(local)
        except ReadLimit as error:
            self.store.settle_attempt(
                self.lease, token, success=False, error=error.public())
            raise
        except XhhError as error:
            self.store.settle_attempt(
                self.lease, token, success=False, error=error.public())
            raise
        except BaseException as error:
            self.store.settle_attempt(
                self.lease,
                token,
                success=False,
                error={
                    "code": "internal_error",
                    "message": str(error),
                    "type": type(error).__name__,
                    "retry_allowed": False,
                },
            )
            raise
        if self.role != "page":
            self.store.settle_attempt(self.lease, token, success=True)
        return value

    def call(self, client, function):
        return self._attempt(
            client.config.protocol_mode,
            lambda local: local._bounded_call(client, function),
        )

    def call_wire(self, function):
        return self._attempt(
            "public",
            lambda local: local.call_wire(function),
        )

    def abort(self, error):
        if self.role == "page" and self.last_token is not None:
            self.store.settle_attempt(
                self.lease,
                self.last_token,
                success=False,
                error=error.public() if isinstance(error, XhhError) else None,
            )

    def view(self):
        return self.store.budget_view(self.lease)


@dataclass(frozen=True)
class QueryHandler:
    command: str

    def initial_position(self) -> Position:
        return Position("offset", 0)

    def argv(self, query: QuerySpec, position: Position,
             page_size: int) -> list[str]:
        if position.mode != "offset" or type(position.value) is not int:
            raise XhhConfigError("corpus position is not an offset")
        if self.command == "feed":
            result = [
                "feed", query.target["topic_id"],
                "--offset", str(position.value),
                "--limit", str(page_size),
            ]
        elif self.command == "search":
            result = [
                "search", query.target["query"], "--type", "link",
                "--offset", str(position.value),
                "--limit", str(page_size),
            ]
        else:
            result = [
                "hashtags", "links", query.target["hashtag_id"],
                "--name", query.target["hashtag_name"],
                "--offset", str(position.value),
                "--limit", str(page_size),
            ]
        filters = query.filters
        for name in (
                "order", "time_range", "community", "content_type",
                "after", "min_comments", "sort"):
            if name in filters:
                result.extend(["--" + name.replace("_", "-"), str(filters[name])])
        for key, value in sorted(
                filters.get("platform_filters", {}).items()):
            result.extend(["--platform-filter", f"{key}={value}"])
        return result

    def next_position(self, coverage) -> Position | None:
        value = coverage.get("next")
        if value is None:
            return None
        if type(value) is not int or value < 0:
            raise XhhConfigError(
                "corpus provider returned an invalid offset continuation")
        return Position("offset", value)


QUERY_HANDLERS = {
    "feed": QueryHandler("feed"),
    "search": QueryHandler("search"),
    "hashtag_links": QueryHandler("hashtags"),
}


class CorpusReader:
    def __init__(self, parser, browser):
        self.parser = parser
        self.browser = browser

    def read(
        self,
        query: QuerySpec,
        position: Position,
        page_size: int,
        budget: CorpusBudget,
    ) -> PageResult:
        handler = QUERY_HANDLERS[query.kind]
        args = self.parser().parse_args(
            handler.argv(query, position, page_size))
        data, coverage = self.browser.page(args, budget, lossless=True)
        source = data.get("source")
        if not isinstance(source, dict):
            raise XhhConfigError(
                "corpus page has no source evidence")
        occurrences = data.get("occurrences")
        if not isinstance(occurrences, list):
            raise XhhConfigError(
                "corpus page did not retain provider occurrences")
        items = []
        for ordinal, occurrence in enumerate(occurrences):
            content = occurrence.get("content")
            reason = occurrence.get("reason")
            entity_key = (
                f"post:{content['id']}"
                if isinstance(content, dict) and content.get("id") else None
            )
            items.append(ProviderItem(
                ordinal=ordinal,
                content=content if isinstance(content, dict) else None,
                entity_key=entity_key,
                digest=occurrence["digest"],
                eligible=entity_key is not None and reason is None,
                reason=reason,
            ))
        evidence = PageEvidence(
            route=source.get("route", ""),
            protocol=source.get("protocol", ""),
            observed_at=float(source.get("observed_at") or 0),
            configured_uid=str(source.get("configured_uid") or ""),
            identity_verified=source.get("identity_verified") is True,
            session_revision=source.get("session_revision"),
        )
        next_position = handler.next_position(coverage)
        if coverage.get("completion") == "complete":
            next_position = None
        return PageResult(
            items=tuple(items),
            coverage=coverage,
            evidence=(evidence,),
            current_position=position,
            next_position=next_position,
        )


def stop_for_error(error) -> StopDecision:
    if isinstance(error, ReadLimit):
        return StopDecision(
            "paused_budget",
            error.code,
            f"corpus paused on {error.reason}",
            True,
        )
    if isinstance(error, (
            XhhCaptchaError, XhhRateLimitError, XhhAuthError,
            XhhSignerError)):
        return StopDecision(
            "blocked_human",
            getattr(error, "code", "platform_error"),
            str(error),
            False,
            fix=getattr(error, "fix", None),
        )
    if isinstance(error, XhhAPIError):
        public = error.public()
        if error.retry_allowed:
            return StopDecision(
                "paused_retryable",
                public.get("code") or "platform_error",
                str(error),
                True,
                fix=public.get("fix"),
            )
        return StopDecision(
            "blocked_human",
            public.get("code") or "platform_error",
            str(error),
            False,
            fix=public.get("fix"),
        )
    if isinstance(error, XhhTransportError):
        return StopDecision(
            "paused_retryable", error.code, str(error), True,
            fix=error.fix)
    if isinstance(error, XhhConfigError):
        code = getattr(error, "code", "configuration_error")
        blocked = code in {
            "session_missing", "session_expired", "signer_error",
            "risk_token_missing", "account_identity_unbound",
            "app_session_missing", "web_session_missing",
            "account_identity_not_verified",
        }
        return StopDecision(
            "blocked_human" if blocked else "failed",
            code, str(error), False, fix=getattr(error, "fix", None))
    if getattr(error, "code", None) == "account_identity_not_verified":
        return StopDecision(
            "blocked_human",
            "account_identity_not_verified",
            str(error),
            False,
            fix="verify the managed account identity and log in again",
        )
    public = (
        error.public() if isinstance(error, XhhError) else {
            "code": "internal_error",
            "message": str(error),
            "retry_allowed": False,
        }
    )
    return StopDecision(
        "failed", public.get("code") or "internal_error",
        public.get("message") or str(error), False,
    )


class CorpusRunner:
    def __init__(
        self,
        store,
        manifest: CorpusManifest,
        lease,
        *,
        parser,
        client_loader,
        browser_factory,
    ):
        self.store = store
        self.manifest = manifest
        self.lease = lease
        self.parser = parser
        self.client_loader = client_loader
        self.browser_factory = browser_factory
        self.client = None
        self.reader = None

    def _delivery_tail(self, query_id):
        delivery = self.store.consume_remainder(self.lease, query_id)
        if delivery is not None:
            delivery["event"] = "delivery"
        return delivery

    def _stop(self, error, query_id=None):
        decision = stop_for_error(error)
        update_query = (
            query_id if not isinstance(error, ReadLimit) else None
        )
        if decision.query_id is None and update_query is not None:
            decision = StopDecision(
                decision.next_state, decision.code, decision.message,
                decision.retry_allowed, decision.not_before, decision.fix,
                update_query, decision.phase,
            )
        status = self.store.stop(self.lease, decision)
        return {
            "event": "status",
            "status": status,
            "error": decision.public(),
        }

    def _pause(self, code, message, query_id=None):
        decision = StopDecision(
            "paused_budget", code, message, True, query_id=query_id)
        status = self.store.stop(self.lease, decision)
        return {
            "event": "status",
            "status": status,
            "error": decision.public(),
        }

    def run(self):
        while True:
            token = self.store.next_staged_token(self.lease)
            if token is None:
                break
            try:
                delivery = self.store.commit_page(
                    self.lease, token=token)
            except Exception as error:
                yield self._stop(error, token.query_id)
                return self.store.status(self.lease["run_id"])
            delivery["event"] = "delivery"
            yield delivery
        status = self.store.status(self.lease["run_id"])
        for query in status["queries"]:
            if query["pending_remainder_count"]:
                delivery = self._delivery_tail(query["query_id"])
                if delivery is not None:
                    yield delivery
        status = self.store.status(self.lease["run_id"])
        actionable = [
            query for query in status["queries"]
            if query["state"] not in {"complete", "quota_reached"}
        ]
        if not actionable:
            if status["run"]["state"] == "complete":
                return status
            return self.store.finish(self.lease)
        if (status["budget"]["unique_records"]["used"]
                >= self.manifest.limits.unique_records):
            yield self._pause(
                "unique_record_budget_exhausted",
                "corpus reached its manifest unique-record quota",
                actionable[0]["query_id"],
            )
            return self.store.status(self.lease["run_id"])
        view = self.store.budget_view(self.lease)
        if (view["invocation"]["new_records_used"]
                >= view["invocation"]["new_record_limit"]):
            yield self._pause(
                "new_record_budget",
                "corpus reached the invocation new-record cap",
                actionable[0]["query_id"],
            )
            return self.store.status(self.lease["run_id"])
        if self.lease["request_limit"] <= 0:
            yield self._pause(
                "request_budget",
                "corpus invocation request budget is exhausted",
                actionable[0]["query_id"],
            )
            return self.store.status(self.lease["run_id"])
        if self.lease["time_limit_ms"] <= 0:
            yield self._pause(
                "time_budget",
                "corpus invocation time budget is exhausted",
                actionable[0]["query_id"],
            )
            return self.store.status(self.lease["run_id"])
        try:
            self.client = self.client_loader()
            if self.client.config.heybox_id != self.manifest.account_uid:
                raise XhhConfigError(
                    "managed account identity does not match corpus manifest",
                    code="account_identity_not_verified",
                )
            self.reader = CorpusReader(
                self.parser, self.browser_factory(self.client))
            from operations import verify_identity
            identity_budget = CorpusBudget(
                self.store,
                self.lease,
                role="identity",
                protocol=self.client.config.protocol_mode,
            )
            identity_budget.call(
                self.client, lambda: verify_identity(self.client))
            self.reader.browser.verified_client_ids.add(id(self.client))
        except Exception as error:
            yield self._stop(error)
            return self.store.status(self.lease["run_id"])

        visited = set()
        while True:
            status = self.store.status(self.lease["run_id"])
            if status["run"]["state"] in {
                    "complete", "blocked_human", "failed", "paused_budget"}:
                return status
            query = next((
                item for item in status["queries"]
                if item["state"] not in {"complete", "quota_reached"}
            ), None)
            if query is None:
                return self.store.finish(self.lease)
            query_id = query["query_id"]
            if query["pending_remainder_count"]:
                delivery = self._delivery_tail(query_id)
                if delivery is not None:
                    yield delivery
                continue
            view = self.store.budget_view(self.lease)
            if (self.store.status(
                    self.lease["run_id"])["budget"]["unique_records"]["used"]
                    >= self.manifest.limits.unique_records):
                yield self._pause(
                    "unique_record_budget_exhausted",
                    "corpus reached its manifest unique-record quota",
                    query_id,
                )
                return self.store.status(self.lease["run_id"])
            if (view["invocation"]["new_records_used"]
                    >= view["invocation"]["new_record_limit"]):
                yield self._pause(
                    "new_record_budget",
                    "corpus reached the invocation new-record cap",
                    query_id,
                )
                return self.store.status(self.lease["run_id"])
            if query["pages_used"] >= query["page_limit"]:
                continue
            position_value = (
                query["next_position"] or query["current_position"])
            position = Position(
                position_value.get("mode", "offset"),
                position_value.get("value"),
            )
            spec = next(
                item for item in self.manifest.queries
                if item.query_id == query_id)
            key = frame_key(
                spec, position, self.manifest.reader)
            existing = self.store.find_frame(
                self.lease["run_id"], key)
            if existing is not None:
                if key in visited:
                    error = XhhConfigError(
                        "corpus frame did not advance the query",
                        code="continuation_missing",
                    )
                    yield self._stop(error, query_id)
                    return self.store.status(self.lease["run_id"])
                visited.add(key)
                continue
            budget = CorpusBudget(
                self.store,
                self.lease,
                query_id=query_id,
                frame_key=key,
                role="page",
                protocol=self.client.config.protocol_mode,
            )
            try:
                page = self.reader.read(
                    spec, position, self.manifest.reader.page_size, budget)
            except Exception as error:
                budget.abort(error)
                yield self._stop(error, query_id)
                return self.store.status(self.lease["run_id"])
            try:
                delivery = self.store.commit_page(
                    self.lease, query_id, budget.last_token, page)
                delivery["event"] = "delivery"
                yield delivery
            except Exception as error:
                yield self._stop(error, query_id)
                return self.store.status(self.lease["run_id"])
