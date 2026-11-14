"""Host-provided run scope.

The skill performs platform operations; it does not verify scheduler identity.
A host agent (Alma or any other) proves the run and hands the result over as a
signed envelope in the environment. Nothing here reads the host's database,
job configuration, hooks, persona files, memory, or emotion records.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass

ENV_SCOPE = "XHH_RUN_SCOPE"
ENV_SECRET = "XHH_RUN_SCOPE_SECRET"

# A host that delegates identity verification cannot be trusted with self-asserted
# limits: these caps bound what any envelope may claim.
MAX_REQUEST_LIMIT = 120
MAX_WRITE_LIMIT = 20
MIN_WRITE_INTERVAL = 15
MAX_SECONDS = 3600
ACCOUNT_ALIASES = ("alt", "main")


class HostProblem(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RunScope:
    run_id: str
    started_at: float
    deadline: float
    contract_version: int = 2
    request_limit: int = MAX_REQUEST_LIMIT
    write_limit: int = MAX_WRITE_LIMIT
    minimum_write_interval: float = MIN_WRITE_INTERVAL
    account_alias: str = "alt"
    account_uid: str | None = None


def _canonical(payload: dict) -> bytes:
    body = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _bounded_int(payload: dict, key: str, upper: int, default: int) -> int:
    value = payload.get(key, default)
    if type(value) is not int or not 0 < value <= upper:
        raise HostProblem("host_scope_invalid")
    return value


def _bounded_float(payload: dict, key: str, default: float) -> float:
    value = payload.get(key, default)
    if type(value) is not float or not 0 < value < 1e9:
        raise HostProblem("host_scope_invalid")
    return value


def decode_scope(envelope: str, secret: str) -> RunScope:
    """Verify and decode a host envelope. Any deviation is a refusal, never a guess."""
    if not isinstance(envelope, str) or not envelope or len(envelope) > 8192:
        raise HostProblem("host_scope_missing")
    if not isinstance(secret, str) or len(secret) < 16 or len(secret) > 512:
        raise HostProblem("host_scope_secret_missing")
    try:
        payload = json.loads(base64.b64decode(envelope, validate=True).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise HostProblem("host_scope_invalid") from None
    if not isinstance(payload, dict):
        raise HostProblem("host_scope_invalid")
    signature = payload.get("signature")
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise HostProblem("host_scope_unsigned")
    expected = hmac.new(secret.encode("utf-8"), _canonical(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HostProblem("host_scope_signature_mismatch")

    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        raise HostProblem("host_scope_invalid")
    started = payload.get("started_at")
    deadline = payload.get("deadline")
    now = payload.get("now")
    if any(type(value) not in (int, float) for value in (started, deadline)):
        raise HostProblem("host_scope_invalid")
    started, deadline = float(started), float(deadline)
    if not (0 < started <= deadline and deadline - started <= MAX_SECONDS):
        raise HostProblem("host_scope_invalid")
    # `now` catches a stale envelope without needing a clock the host controls here.
    if type(now) in (int, float) and float(now) > deadline:
        raise HostProblem("host_scope_expired")

    version = payload.get("contract_version", 2)
    if version not in (1, 2):
        raise HostProblem("host_scope_invalid")
    alias = payload.get("account_alias", "alt")
    if alias not in ACCOUNT_ALIASES:
        raise HostProblem("host_scope_invalid")
    uid = payload.get("account_uid")
    if uid is not None and (not isinstance(uid, str) or not re.fullmatch(r"[0-9]{1,32}", uid)):
        raise HostProblem("host_scope_invalid")

    return RunScope(
        run_id=run_id,
        started_at=started,
        deadline=deadline,
        contract_version=version,
        request_limit=_bounded_int(payload, "request_limit", MAX_REQUEST_LIMIT, MAX_REQUEST_LIMIT),
        write_limit=_bounded_int(payload, "write_limit", MAX_WRITE_LIMIT, MAX_WRITE_LIMIT),
        minimum_write_interval=_bounded_float(payload, "minimum_write_interval", float(MIN_WRITE_INTERVAL)),
        account_alias=alias,
        account_uid=uid,
    )


def current_run_scope(environment=None) -> RunScope | None:
    """Present a verified host scope, or None when the host sent none.

    No envelope means an ordinary interactive run: the skill does not try to
    infer one from the host's own files.
    """
    env = os.environ if environment is None else environment
    envelope = env.get(ENV_SCOPE)
    if not envelope:
        return None
    return decode_scope(envelope, env.get(ENV_SECRET) or "")
