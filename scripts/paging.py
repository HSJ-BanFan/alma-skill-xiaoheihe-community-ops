from __future__ import annotations

import base64
import copy
import json
import time
from dataclasses import dataclass
from enum import Enum

from documents import integer, truth
from xhh_sdk.exceptions import XhhBudgetError, XhhConfigError
from xhh_sdk.transport import Transport
from xhh_sdk.signer import Signer
from network_io import read_http, NetworkDeadline, NetworkRequestLimit

_BLOCK_CODES = frozenset({"captcha_required", "rate_limited"})
_CAPTCHA_STATUSES = frozenset(
    {"show_captcha", "captcha", "verify_captcha"})
_RATE_STATUSES = frozenset({"http_429", "429"})
_BUDGET_CODES = {
    "request_budget": "request_budget_exhausted",
    "time_budget": "time_budget_exhausted",
    "page_budget": "page_budget_exhausted",
    "item_budget": "item_budget_exhausted",
}


def _classify_block(value, *, stop_reason=None):
    error = dict(value) if isinstance(value, dict) else {}
    code = error.get("code")
    status = str(error.get("status") or "").casefold()
    budget = stop_reason in _BUDGET_CODES or code in _BUDGET_CODES.values()
    blocked = (
        error.get("retry_allowed") is False
        and stop_reason == "platform_error"
        and not budget
        or code in _BLOCK_CODES
        or status in _CAPTCHA_STATUSES
        or status in _RATE_STATUSES
    )
    if status in _CAPTCHA_STATUSES or code == "captcha_required":
        error["code"] = "captcha_required"
        error["retry_allowed"] = False
    elif status in _RATE_STATUSES or code == "rate_limited":
        error["code"] = "rate_limited"
        error["retry_allowed"] = False
    return blocked, error


class ReadLimit(XhhBudgetError):
    def __init__(self, reason):
        self.reason = reason
        code = _BUDGET_CODES.get(reason, "read_budget_exhausted")
        super().__init__(
            reason,
            code=code,
            fix="use the returned partial data and resume token, or explicitly increase the budget",
        )


class Budget:
    def __init__(self, requests=10, seconds=30, pages=100, items=100):
        self.request_limit = requests
        self.deadline = time.monotonic() + seconds
        self.page_limit = pages
        self.item_limit = items
        self.used = 0
        self.pages_used = 0
        self.items_used = 0

    def _start(self):
        remaining = self.deadline - time.monotonic()
        if self.used >= self.request_limit:
            raise ReadLimit("request_budget")
        if remaining <= 0:
            raise ReadLimit("time_budget")
        starting_used = self.used
        self.used += 1
        return starting_used, remaining

    def _wire_tracking(self, starting_used):
        wire_limit = self.request_limit - starting_used

        def record_wire_requests(count):
            if type(count) is not int or count < 0 or count > wire_limit:
                raise NetworkRequestLimit()
            self.used = starting_used + count

        return wire_limit, record_wire_requests

    def call(self, client, function):
        starting_used, remaining = self._start()
        return self._bounded_call(
            client, function, starting_used=starting_used)

    def _bounded_call(self, client, function, *, starting_used=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ReadLimit("time_budget")
        if starting_used is None:
            starting_used = self.used
        wire_used = 0
        original = client.config.timeout
        client.config.timeout = min(original, remaining)
        transport = client.transport
        original_open = (getattr(transport, "_open", None)
                         if isinstance(transport, Transport) else None)
        signer, signer_timeout = None, None

        def before_http(request):
            nonlocal wire_used
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ReadLimit("time_budget")
            client.config.timeout = min(original, remaining)
            wire_limit = self.request_limit - starting_used - wire_used
            if wire_limit <= 0:
                raise ReadLimit("request_budget")

            def record_wire_requests(count):
                nonlocal wire_used
                if (type(count) is not int or count < 0
                        or count > wire_limit):
                    raise NetworkRequestLimit()
                wire_used += count
                self.used = starting_used + wire_used

            try:
                data = read_http(
                    request,
                    remaining,
                    http_timeout=client.config.timeout,
                    wire_limit=wire_limit,
                    wire_counter=record_wire_requests,
                )
            except NetworkRequestLimit:
                raise ReadLimit("request_budget") from None
            except NetworkDeadline:
                raise ReadLimit("time_budget") from None
            return data

        try:
            if original_open is not None:
                transport._open = before_http
            if client.config.protocol_mode == "app" and isinstance(transport, Transport):
                candidate = transport.signer
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise ReadLimit("time_budget")
                if isinstance(candidate, Signer):
                    signer, signer_timeout = candidate, candidate.timeout
                    signer.timeout = min(signer_timeout, remaining)
            value = function()
            if time.monotonic() >= self.deadline:
                raise ReadLimit("time_budget")
            return value
        finally:
            if original_open is not None:
                transport._open = original_open
            if signer_timeout is not None:
                signer.timeout = signer_timeout
            client.config.timeout = original

    def call_wire(self, function):
        starting_used, remaining = self._start()
        wire_limit, record_wire_requests = self._wire_tracking(starting_used)
        try:
            value = function(remaining, wire_limit, record_wire_requests)
        except NetworkRequestLimit:
            raise ReadLimit("request_budget") from None
        except NetworkDeadline:
            raise ReadLimit("time_budget") from None
        if time.monotonic() >= self.deadline:
            raise ReadLimit("time_budget")
        return value

    def reserve_page(self):
        if self.pages_used >= self.page_limit:
            raise ReadLimit("page_budget")
        if self.deadline - time.monotonic() <= 0:
            raise ReadLimit("time_budget")
        self.pages_used += 1

    def consume_items(self, count):
        if count < 0 or self.items_used + count > self.item_limit:
            raise ReadLimit("item_budget")
        self.items_used += count

    def view(self):
        return {"used_requests": self.used, "request_limit": self.request_limit,
                "seconds_remaining": max(0, round(self.deadline - time.monotonic(), 3)),
                "pages_used": self.pages_used, "page_limit": self.page_limit,
                "items_used": self.items_used, "item_limit": self.item_limit}


class StopKind(str, Enum):
    COMPLETE = "complete"
    DATA_GAP = "data_gap"
    BUDGET_PAUSE = "budget_pause"
    BLOCKED = "blocked"
    RETRYABLE_FAILURE = "retryable_failure"
    FATAL = "fatal"
    CURSOR_STALLED = "cursor_stalled"


@dataclass(frozen=True)
class StopState:
    kind: StopKind
    reason: str | None = None
    error: dict | None = None

    @classmethod
    def complete(cls):
        return cls(StopKind.COMPLETE)

    @classmethod
    def budget_pause(cls, reason):
        return cls(StopKind.BUDGET_PAUSE, reason=reason)

    @classmethod
    def from_error(cls, error):
        blocked, public = _classify_block(
            error.public(), stop_reason="platform_error")
        if blocked:
            return cls(
                StopKind.BLOCKED, reason="platform_error", error=public)
        return cls(StopKind.RETRYABLE_FAILURE, reason="platform_error",
                   error=public)

    @classmethod
    def cursor_stalled(cls):
        return cls(StopKind.CURSOR_STALLED, reason="cursor_stalled")

    @classmethod
    def fatal(cls, reason, error=None):
        return cls(StopKind.FATAL, reason=reason, error=error)


_STATUS = {
    StopKind.COMPLETE: "complete",
    StopKind.DATA_GAP: "partial",
    StopKind.BUDGET_PAUSE: "partial",
    StopKind.BLOCKED: "blocked",
    StopKind.RETRYABLE_FAILURE: "retryable_failure",
    StopKind.FATAL: "failed",
    StopKind.CURSOR_STALLED: "cursor_stalled",
}


def materialize_resume(stop, *, command, uid, protocol, binding,
                       parameters):
    if stop.kind not in {
            StopKind.BUDGET_PAUSE, StopKind.RETRYABLE_FAILURE}:
        return None
    if parameters is None:
        return None
    return encode_resume(
        command,
        uid,
        {"binding": binding, "protocol": protocol, **parameters},
    )


def finalize_coverage(scope, stop, *, command, uid, protocol, binding,
                      resume_parameters=None):
    scope["status"] = _STATUS[stop.kind]
    if stop.kind == StopKind.COMPLETE:
        scope.pop("stop_reason", None)
        scope.pop("error", None)
    elif stop.reason:
        scope["stop_reason"] = stop.reason
    if stop.error:
        scope["error"] = stop.error
    elif stop.kind in {
            StopKind.COMPLETE, StopKind.BUDGET_PAUSE,
            StopKind.CURSOR_STALLED}:
        scope.pop("error", None)
    scope["resume"] = materialize_resume(
        stop,
        command=command,
        uid=uid,
        protocol=protocol,
        binding=binding,
        parameters=resume_parameters,
    )
    return scope


def delivery_ok(coverage):
    return coverage.get("status") in {"complete", "partial"}


def project_coverage(value):
    coverage = copy.deepcopy(value) if isinstance(value, dict) else {}
    status = coverage.get("status")
    source_error = (
        coverage.get("error")
        if isinstance(coverage.get("error"), dict)
        else {}
    )
    stop_reason = coverage.get("stop_reason")
    blocked, normalized_error = _classify_block(source_error, stop_reason=stop_reason)
    if blocked:
        coverage["error"] = normalized_error
        coverage["status"] = "blocked"
        coverage["resume"] = None
        return coverage
    if stop_reason in _BUDGET_CODES or source_error.get("code") in _BUDGET_CODES.values():
        coverage["status"] = "partial"
        coverage.pop("error", None)
        return coverage
    if status in {
            "complete", "partial", "blocked", "retryable_failure",
            "failed", "cursor_stalled"}:
        if status not in {"partial", "retryable_failure"}:
            coverage["resume"] = None
        return coverage

    completion = coverage.get("completion")
    error = source_error
    retry_allowed = error.get("retry_allowed")
    if stop_reason == "cursor_stalled":
        status = "cursor_stalled"
    elif stop_reason == "platform_error":
        status = ("retryable_failure" if retry_allowed is True
                  else "blocked")
    elif completion == "complete":
        status = "complete"
    elif stop_reason:
        status = "failed"
    elif completion in {"more", "unknown"}:
        status = "partial"
    else:
        status = "failed"
    coverage["status"] = status
    if status not in {"partial", "retryable_failure"}:
        coverage["resume"] = None
    return coverage


def coverage(result, *, count, position, mode="offset"):
    more = truth(result.get("has_more_floors", result.get("has_more")))
    total_pages = integer(result.get("total_page"))
    if mode == "page" and total_pages is not None:
        from_total = position < total_pages
        if more is not None and more != from_total:
            return {"completion": "unknown", "has_more": None, "gaps": ["conflicting_page_markers"], "next": None}
        more = from_total
    cursor = result.get("lastval")
    if mode == "cursor":
        next_position = str(cursor) if cursor not in (None, "") else None
    else:
        next_position = position + 1 if mode == "page" and more is True else position + (1 if mode == "page" else count) if count else None
    if more is True and next_position is None:
        gaps = ["continuation_missing"]
    else:
        gaps = []
    return {"completion": "complete" if more is False else "more" if more is True else "unknown",
            "has_more": more, "gaps": gaps, "next": None if more is False else next_position,
            "position": position, "mode": mode, "returned": count, "total_pages": total_pages,
            "full_history": False}


def encode_resume(command, uid, parameters):
    return base64.urlsafe_b64encode(json.dumps({"version": 1, "command": command, "uid": uid,
                    "parameters": parameters}, separators=(",", ":")).encode()).decode()


def decode_resume(token, command, uid):
    try:
        if not isinstance(token, str) or len(token) > 16000:
            raise ValueError()
        value = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
        if not isinstance(value, dict) or value.get("version") != 1 or value.get("command") != command or value.get("uid") != uid:
            raise ValueError()
        parameters = value["parameters"]
        if not isinstance(parameters, dict):
            raise ValueError()
        return parameters
    except (ValueError, TypeError, KeyError):
        raise XhhConfigError("resume token is invalid or belongs to a different command/account") from None
