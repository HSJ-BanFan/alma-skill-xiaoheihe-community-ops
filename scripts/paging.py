"""Bounded reads and evidence-based continuation positions."""
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
from network_io import read_http, NetworkDeadline


class ReadLimit(XhhBudgetError):
    def __init__(self, reason):
        self.reason = reason
        code = {
            "request_budget": "request_budget_exhausted",
            "time_budget": "time_budget_exhausted",
            "page_budget": "page_budget_exhausted",
            "item_budget": "item_budget_exhausted",
        }.get(reason, "read_budget_exhausted")
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

    def call(self, client, function):
        remaining = self.deadline - time.monotonic()
        if self.used >= self.request_limit:
            raise ReadLimit("request_budget")
        if remaining <= 0:
            raise ReadLimit("time_budget")
        original = client.config.timeout
        client.config.timeout = min(original, remaining)
        self.used += 1
        transport = client.transport
        original_open = (getattr(transport, "_open", None)
                         if isinstance(transport, Transport) else None)
        signer, signer_timeout = None, None

        def before_http(request):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ReadLimit("time_budget")
            client.config.timeout = min(original, remaining)
            try:
                data = read_http(request, remaining, http_timeout=client.config.timeout)
            except NetworkDeadline:
                raise ReadLimit("time_budget") from None
            if time.monotonic() >= self.deadline:
                raise ReadLimit("time_budget")
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
            return function()
        finally:
            if original_open is not None:
                transport._open = original_open
            if signer_timeout is not None:
                signer.timeout = signer_timeout
            client.config.timeout = original

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
        public = error.public()
        code = getattr(error, "code", None)
        status = str(getattr(error, "status", "") or "").casefold()
        if status in {"show_captcha", "captcha", "verify_captcha"}:
            public["code"] = "captcha_required"
            public["retry_allowed"] = False
        elif status in {"http_429", "429"} or code == "rate_limited":
            public["code"] = "rate_limited"
            public["retry_allowed"] = False
        if (not error.retry_allowed
                or code in {"captcha_required", "rate_limited"}
                or status in {
                    "show_captcha", "captcha", "verify_captcha",
                    "http_429", "429",
                }):
            return cls(StopKind.BLOCKED, reason="platform_error", error=public)
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
    if status in {
            "complete", "partial", "blocked", "retryable_failure",
            "failed", "cursor_stalled"}:
        if status not in {"partial", "retryable_failure"}:
            coverage["resume"] = None
        return coverage

    completion = coverage.get("completion")
    stop_reason = coverage.get("stop_reason")
    error = coverage.get("error") if isinstance(
        coverage.get("error"), dict) else {}
    code = error.get("code")
    retry_allowed = error.get("retry_allowed")
    blocked = (
        retry_allowed is False
        or code in {"captcha_required", "rate_limited"}
        or str(error.get("status") or "").casefold() in {
            "show_captcha", "captcha", "verify_captcha", "http_429", "429",
        }
    )
    if blocked and code != "rate_limited":
        status_value = str(error.get("status") or "").casefold()
        if status_value in {"show_captcha", "captcha", "verify_captcha"}:
            coverage["error"]["code"] = "captcha_required"
            coverage["error"]["retry_allowed"] = False
        elif status_value in {"http_429", "429"}:
            coverage["error"]["code"] = "rate_limited"
            coverage["error"]["retry_allowed"] = False
    if stop_reason in {"request_budget", "time_budget", "page_budget",
                       "item_budget"}:
        status = "partial"
    elif blocked:
        status = "blocked"
    elif stop_reason == "cursor_stalled":
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
