"""Exception hierarchy for xhh_sdk.

Web post diagnostics carry closed enum values and project fixed public fields.
"""
from __future__ import annotations

from enum import Enum


class SDKDiagnostic(Enum):
    WEB_POST_MISSING_PARAMETER = "web_post_missing_parameter"
    WEB_POST_PUBLISHING_UNAVAILABLE = "web_post_publishing_unavailable"

    def public_fields(self) -> dict[str, str]:
        return {
            "code": self.value,
            "path": "/bbs/app/api/link/post",
            "status": "failed",
            "message": {
                SDKDiagnostic.WEB_POST_MISSING_PARAMETER: "缺失参数",
                SDKDiagnostic.WEB_POST_PUBLISHING_UNAVAILABLE: "发布功能暂不可用",
            }[self],
        }


class XhhError(Exception):
    """Base class for every SDK error."""


class XhhConfigError(XhhError):
    """Configuration is missing or invalid."""


class XhhSignerError(XhhError):
    """The offline signer could not produce a signature."""


class XhhTransportError(XhhError):
    """Network-level failure (DNS, TLS, timeout, connection reset)."""


class XhhAuthError(XhhError):
    """The platform rejected the session (login required or expired)."""


class XhhRateLimitError(XhhError):
    """The platform throttled this action (发帖频率过快, code 10006)."""


class XhhAPIError(XhhError):
    """The platform answered with a non-ok status."""

    def __init__(self, message: str, *, status: str = "", path: str = "",
                 diagnostic: SDKDiagnostic | None = None):
        super().__init__(message)
        self.status = status
        self.path = path
        self.diagnostic = diagnostic
