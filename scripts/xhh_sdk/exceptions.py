"""Exception hierarchy for xhh_sdk.

Every error carries a public, printable message. Credentials, cookies and raw
upstream bodies never appear in exception text.
"""
from __future__ import annotations


class XhhError(Exception):
    """Base class for every SDK error."""

    code = "xhh_error"
    default_fix = ""
    retry_allowed = True

    def __init__(self, message: str, *, code: str | None = None,
                 fix: str | None = None, retry_allowed: bool | None = None):
        super().__init__(message)
        self.code = code or self.code
        self.fix = fix if fix is not None else self.default_fix
        self.retry_allowed = (self.retry_allowed
                              if retry_allowed is None else retry_allowed)

    def public(self) -> dict:
        value = {"code": self.code, "message": str(self),
                 "type": type(self).__name__,
                 "retry_allowed": self.retry_allowed}
        if self.fix:
            value["fix"] = self.fix
        return value


class XhhConfigError(XhhError):
    """Configuration is missing or invalid."""

    code = "configuration_error"
    retry_allowed = False


class XhhSignerError(XhhError):
    """The offline signer could not produce a signature."""

    code = "signer_error"
    default_fix = "verify the signer path, SHA-256, Java installation, and account/device binding"
    retry_allowed = False


class XhhTransportError(XhhError):
    """Network-level failure (DNS, TLS, timeout, connection reset)."""

    code = "transport_error"
    default_fix = "check network access, then retry the bounded read"


class XhhAuthError(XhhError):
    """The platform rejected the session (login required or expired)."""

    code = "session_expired"
    default_fix = "run account status --online, then log in again for the selected protocol"
    retry_allowed = False


class XhhRateLimitError(XhhError):
    """The platform throttled this action (发帖频率过快, code 10006)."""

    code = "rate_limited"
    default_fix = "stop the current task and wait for the platform limit to clear"
    retry_allowed = False


class XhhCaptchaError(XhhError):
    """The platform requires interactive human verification."""

    code = "captcha_required"
    default_fix = "stop all requests; wait for the platform cooldown and complete verification interactively"
    retry_allowed = False


class XhhRiskTokenError(XhhConfigError):
    """The App SMS login flow lacks its anti-fraud cookie."""

    code = "risk_token_missing"
    default_fix = "capture the account's x_xhh_tokenid with account risk-token, then retry login"


class XhhIdentityUnboundError(XhhConfigError):
    """An App login needs a numeric identity before it can sign."""

    code = "account_identity_unbound"
    default_fix = "bind the account identity or supply --identity during login"


class XhhSessionMissingError(XhhConfigError):
    """The requested protocol has no stored session for this account."""

    code = "session_missing"
    default_fix = "log in separately for the requested App or Web protocol"


class XhhSessionExpiredError(XhhAuthError):
    """The platform rejected the selected protocol session."""

    code = "session_expired"
    default_fix = "log in again for the selected protocol"


class XhhBudgetError(XhhError):
    """A caller-defined request, page, item, or time budget stopped the read."""

    code = "request_budget_exhausted"
    default_fix = "use the returned resume token or increase the explicit budget"
    retry_allowed = True


class XhhAPIError(XhhError):
    """The platform answered with a non-ok status."""

    code = "platform_error"

    def __init__(self, message: str, *, status: str = "", path: str = "",
                 code: str | None = None, fix: str | None = None,
                 retry_allowed: bool | None = None):
        super().__init__(message, code=code, fix=fix, retry_allowed=retry_allowed)
        self.status = status
        self.path = path

    def public(self) -> dict:
        value = super().public()
        if self.status:
            value["status"] = self.status
        if self.path:
            value["path"] = self.path
        return value
