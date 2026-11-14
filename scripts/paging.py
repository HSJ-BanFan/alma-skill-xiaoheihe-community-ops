"""Bounded reads and evidence-based continuation positions."""
from __future__ import annotations

import base64
import json
import time

from documents import integer, truth
from xhh_sdk.exceptions import XhhConfigError
from xhh_sdk.transport import Transport
from xhh_sdk.signer import Signer
from network_io import read_http, NetworkDeadline


class ReadLimit(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class Budget:
    def __init__(self, requests=10, seconds=30):
        self.request_limit = requests
        self.deadline = time.monotonic() + seconds
        self.used = 0

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
        original_open = getattr(transport, "_open", None)
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

    def view(self):
        return {"used_requests": self.used, "request_limit": self.request_limit,
                "seconds_remaining": max(0, round(self.deadline - time.monotonic(), 3))}


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
