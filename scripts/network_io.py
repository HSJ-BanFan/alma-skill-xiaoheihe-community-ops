"""Bound the complete existing HTTP reader in a terminable process."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xhh_sdk.exceptions import XhhAPIError, XhhAuthError, XhhConfigError, XhhTransportError

NETWORK_ENV = {"PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
               "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}


class NetworkDeadline(RuntimeError):
    pass


def read_http(request, timeout, *, profile="api", _command=None):
    if request.get_method() != "GET" or request.data is not None or not request.full_url.startswith("https://"):
        raise XhhConfigError("bounded reader accepts HTTPS GET only")
    payload = {"url": request.full_url, "headers": dict(request.header_items()), "timeout": timeout, "profile": profile}
    command = _command or [sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve())]
    environment = {key: value for key, value in os.environ.items() if key.upper() in NETWORK_ENV}
    try:
        result = subprocess.run(command, input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, env=environment, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except subprocess.TimeoutExpired:
        raise NetworkDeadline("HTTP response deadline exceeded") from None
    if result.returncode != 0:
        raise XhhTransportError("bounded reader process failed")
    try:
        value = json.loads(result.stdout)
        if value["ok"]:
            return base64.b64decode(value["body"], validate=True)
        kind, status = value.get("kind"), value.get("status") or ""
    except (ValueError, KeyError, TypeError):
        raise XhhTransportError("bounded reader protocol failed") from None
    if kind == "XhhAuthError":
        raise XhhAuthError("platform rejected the session")
    if kind in ("XhhAPIError", "HTTPError"):
        raise XhhAPIError("HTTP read rejected", status=status)
    if kind == "XhhConfigError":
        raise XhhConfigError("bounded reader request invalid")
    raise XhhTransportError("HTTP read failed" + (": " + status if status else ""))


def worker():
    try:
        value = json.loads(sys.stdin.buffer.read())
        timeout = float(value["timeout"])
        if not 0 < timeout <= 120 or value["profile"] not in ("api", "asset"):
            raise XhhConfigError("invalid reader request")
        request = urllib.request.Request(value["url"], headers=value["headers"], method="GET")
        if not request.full_url.startswith("https://"):
            raise XhhConfigError("HTTPS required")
        if value["profile"] == "api":
            from xhh_sdk.config import XhhConfig
            from xhh_sdk.transport import Transport
            transport = Transport(XhhConfig(pkey="request-worker", heybox_id="1", timeout=timeout))
            data = transport._open(request)
        else:
            from assets import platform_url, Redirects, MAX_BYTES
            platform_url(request.full_url)
            if any(key.lower() == "cookie" for key in value["headers"]):
                raise XhhConfigError("asset reader cannot send credentials")
            with urllib.request.build_opener(Redirects()).open(request, timeout=timeout) as response:
                data = response.read(MAX_BYTES + 1)
        result = {"ok": True, "body": base64.b64encode(data).decode("ascii")}
    except Exception as error:
        status = getattr(error, "status", None)
        if status is None and hasattr(error, "code"):
            status = "HTTP_" + str(error.code)
        result = {"ok": False, "kind": type(error).__name__, "status": status}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    worker()
