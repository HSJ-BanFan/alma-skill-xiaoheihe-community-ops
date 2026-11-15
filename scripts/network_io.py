"""Bound the complete existing HTTP reader in a terminable process."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.request
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xhh_sdk.exceptions import (
    XhhAPIError,
    XhhAuthError,
    XhhBudgetError,
    XhhConfigError,
    XhhRateLimitError,
    XhhTransportError,
)

PUBLIC_MAX_BYTES = 4 * 1024 * 1024
NETWORK_ENV = {"PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
               "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}


class NetworkDeadline(RuntimeError):
    pass


class NetworkRequestLimit(XhhBudgetError):
    def __init__(self):
        super().__init__(
            "request",
            code="request_budget_exhausted",
            fix="use the returned partial data and resume token, or explicitly increase the request budget",
        )


@dataclass
class _WireCounter:
    limit: int | None = None
    used: int = 0

    def request(self):
        if self.limit is not None and self.used >= self.limit:
            raise NetworkRequestLimit()
        self.used += 1


class AuthenticatedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, url, counter=None):
        super().__init__()
        self.origin = self._origin(url)
        self.counter = counter or _WireCounter()

    @staticmethod
    def _origin(url):
        value = urllib.parse.urlsplit(url)
        return value.scheme, value.hostname, value.port or 443

    def redirect_request(self, request, fp, code, message, headers, newurl):
        value = urllib.parse.urlsplit(newurl)
        if value.username or value.password or self._origin(newurl) != self.origin:
            raise XhhConfigError("authenticated cross-origin redirect blocked")
        self.counter.request()
        return super().redirect_request(request, fp, code, message, headers, newurl)


class PublicXiaoheiheRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, url=None, *, counter=None, wire_limit=None):
        super().__init__()
        self.counter = counter or _WireCounter(
            limit=wire_limit,
            used=1 if wire_limit is not None else 0,
        )

    @staticmethod
    def _allowed(url):
        value = urllib.parse.urlsplit(url)
        host = (value.hostname or "").casefold()
        return (value.scheme == "https" and not value.username and not value.password
                and (host == "xiaoheihe.cn" or host.endswith(".xiaoheihe.cn")))

    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not self._allowed(newurl):
            raise XhhConfigError("public Xiaoheihe redirect blocked")
        self.counter.request()
        return super().redirect_request(request, fp, code, message, headers, newurl)


def read_http(request, timeout, *, profile="api", http_timeout=None,
              wire_limit=None, wire_counter=None):
    if request.get_method() != "GET" or request.data is not None or not request.full_url.startswith("https://"):
        raise XhhConfigError("bounded reader accepts HTTPS GET only")
    if wire_limit is not None and (type(wire_limit) is not int or wire_limit < 1):
        raise XhhConfigError("wire request limit must be a positive integer")
    payload = {"url": request.full_url, "headers": dict(request.header_items()), "timeout": timeout,
               "http_timeout": min(timeout, http_timeout) if http_timeout is not None else timeout,
               "profile": profile, "wire_limit": wire_limit}
    environment = {key: value for key, value in os.environ.items() if key.upper() in NETWORK_ENV}
    try:
        result = subprocess.run([sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve())], input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, env=environment, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except subprocess.TimeoutExpired:
        raise NetworkDeadline("HTTP response deadline exceeded") from None
    if result.returncode != 0:
        raise XhhTransportError("bounded reader process failed")
    try:
        value = json.loads(result.stdout)
        wire_requests = value.get("wire_requests", 1)
        if type(wire_requests) is not int or wire_requests < 0:
            raise ValueError()
        if wire_counter is not None:
            wire_counter(wire_requests)
        if value["ok"]:
            body = base64.b64decode(value["body"], validate=True)
            if profile == "public" and len(body) > PUBLIC_MAX_BYTES:
                raise XhhAPIError(
                    "public response exceeded the 4 MiB body limit",
                    status="body_limit",
                    code="public_response_too_large",
                    retry_allowed=False,
                )
            return body
        kind, status = value.get("kind"), value.get("status") or ""
    except (ValueError, KeyError, TypeError):
        raise XhhTransportError("bounded reader protocol failed") from None
    if kind == "XhhRateLimitError" or status.upper() == "HTTP_429":
        raise XhhRateLimitError("platform throttled the request")
    if (kind == "XhhAuthError"
            or status.upper() in {"HTTP_401", "HTTP_403"}):
        if profile == "public":
            raise XhhAPIError(
                "public Xiaoheihe access denied",
                status=status,
                code="http_access_denied",
                retry_allowed=False,
            )
        raise XhhAuthError("platform rejected the session")
    if kind == "NetworkRequestLimit":
        raise NetworkRequestLimit()
    if kind in ("XhhAPIError", "HTTPError"):
        try:
            status_code = int(str(status).upper().removeprefix("HTTP_"))
        except ValueError:
            status_code = 0
        raise XhhAPIError(
            "HTTP read rejected",
            status=status,
            code=value.get("code") or "platform_error",
            retry_allowed=(
                value.get("retry_allowed")
                if isinstance(value.get("retry_allowed"), bool)
                else status_code >= 500 or status_code == 408
            ),
        )
    if kind == "XhhConfigError":
        raise XhhConfigError("bounded reader request invalid")
    raise XhhTransportError("HTTP read failed" + (": " + status if status else ""))


def worker():
    wire_counter = _WireCounter()
    try:
        value = json.loads(sys.stdin.buffer.read())
        timeout = float(value["timeout"])
        http_timeout = float(value["http_timeout"])
        if not 0 < http_timeout <= timeout <= 120 or value["profile"] not in ("api", "asset", "public"):
            raise XhhConfigError("invalid reader request")
        wire_limit = value.get("wire_limit")
        if wire_limit is not None and (type(wire_limit) is not int or wire_limit < 1):
            raise XhhConfigError("invalid wire request limit")
        wire_counter.limit = wire_limit
        request = urllib.request.Request(value["url"], headers=value["headers"], method="GET")
        if not request.full_url.startswith("https://"):
            raise XhhConfigError("HTTPS required")
        wire_counter.request()
        if value["profile"] == "api":
            from xhh_sdk.config import XhhConfig
            from xhh_sdk.transport import Transport
            transport = Transport(XhhConfig(pkey="request-worker", heybox_id="1", timeout=http_timeout))
            original = urllib.request.urlopen
            try:
                urllib.request.urlopen = urllib.request.build_opener(
                    AuthenticatedRedirects(request.full_url, wire_counter)).open
                data = transport._open(request)
            finally:
                urllib.request.urlopen = original
        elif value["profile"] == "asset":
            from assets import platform_url, Redirects, MAX_BYTES
            platform_url(request.full_url)
            if any(key.lower() == "cookie" for key in value["headers"]):
                raise XhhConfigError("asset reader cannot send credentials")
            with urllib.request.build_opener(Redirects()).open(request, timeout=http_timeout) as response:
                data = response.read(MAX_BYTES + 1)
        else:
            if not PublicXiaoheiheRedirects._allowed(request.full_url):
                raise XhhConfigError("public reader requires a Xiaoheihe HTTPS URL")
            if any(key.lower() == "cookie" for key in value["headers"]):
                raise XhhConfigError("public reader cannot send credentials")
            with urllib.request.build_opener(
                    PublicXiaoheiheRedirects(counter=wire_counter)).open(
                        request, timeout=http_timeout) as response:
                data = response.read(PUBLIC_MAX_BYTES + 1)
            if len(data) > PUBLIC_MAX_BYTES:
                raise XhhAPIError(
                    "public response exceeded the 4 MiB body limit",
                    status="body_limit",
                    code="public_response_too_large",
                    retry_allowed=False,
                )
        result = {"ok": True, "body": base64.b64encode(data).decode("ascii")}
    except Exception as error:
        status = getattr(error, "status", None)
        if status is None and hasattr(error, "code"):
            status = "HTTP_" + str(error.code)
        result = {"ok": False, "kind": type(error).__name__, "status": status}
        for name in ("code", "retry_allowed"):
            value = getattr(error, name, None)
            if value is not None:
                result[name] = value
    result["wire_requests"] = wire_counter.used
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    worker()
