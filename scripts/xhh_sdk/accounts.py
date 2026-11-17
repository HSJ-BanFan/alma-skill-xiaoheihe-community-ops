"""Explicit account selection with current-user Windows DPAPI storage."""
from __future__ import annotations

import base64
import ctypes as C
from ctypes import wintypes as W
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

from .config import XhhConfig
from .secure_phone import normalize_phone
from .exceptions import (
    XhhConfigError,
    XhhIdentityUnboundError,
    XhhSessionMissingError,
)

_CONFIG_KEYS = {"java", "signer_jar", "signer_bundle", "imei", "device_info",
                "app_version", "os_version", "timeout", "protocol_mode"}
_PROTOCOLS = frozenset({"app", "web"})
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10))}
_MAX_LOGIN_PHONES = 16
_LOGIN_PROTOCOLS = {"sms": "app", "creator": "web", "qr": "web", "browser": "web"}


def _observation_time() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, repr=False, slots=True)
class VerifiedLoginObservation:
    """Candidate login successfully checked against the online account UID."""

    identity: str
    protocol: str
    method: str
    verified_at: str = field(default_factory=_observation_time)
    phone: str | None = field(default=None, repr=False)


@dataclass(frozen=True, repr=False, slots=True)
class LoginPhoneObservation:
    phone: str
    identity: str
    verified_at: str


@dataclass
class ManagedAccount:
    alias: str
    identity: str
    config: dict
    revision: str
    sessions: dict[str, str] = field(default_factory=dict, repr=False)
    risk_token: str | None = field(default=None, repr=False)
    verified_identity: VerifiedLoginObservation | None = field(default=None, repr=False)
    login_phones: list[LoginPhoneObservation] = field(default_factory=list, repr=False)

    def select_session(self, protocol: str | None = None) -> "SessionSelection":
        if protocol is not None:
            if protocol not in _PROTOCOLS:
                raise XhhConfigError("protocol must be app or web")
            if protocol not in self.sessions:
                raise _missing_session(self, protocol)
            return SessionSelection(
                protocol=protocol,
                explicit=True,
                preferred=self.config.get("protocol_mode"),
                fallback=False,
            )

        preferred = self.config.get("protocol_mode")
        if preferred in self.sessions:
            return SessionSelection(
                protocol=preferred,
                explicit=False,
                preferred=preferred,
                fallback=False,
            )
        for candidate in ("app", "web"):
            if candidate in self.sessions:
                return SessionSelection(
                    protocol=candidate,
                    explicit=False,
                    preferred=preferred,
                    fallback=preferred in _PROTOCOLS,
                )
        raise XhhSessionMissingError(
            "Account has no stored App or Web session",
            code="session_missing",
            fix=(
                f"run account login {self.alias} --method sms for App, or "
                f"run account login {self.alias} --method creator for Web"
            ),
        )


@dataclass(frozen=True)
class SessionSelection:
    protocol: str
    explicit: bool
    preferred: str | None
    fallback: bool

    def public(self) -> dict:
        return {
            "protocol": self.protocol,
            "explicit": self.explicit,
            "preferred": self.preferred,
            "fallback": self.fallback,
        }


def _missing_session(account, protocol):
    return XhhSessionMissingError(
        f"Account has no stored {protocol} session",
        code=f"{protocol}_session_missing",
        fix=(
            f"run account login {account.alias} --method "
            + ("sms" if protocol == "app" else "creator")
        ),
    )


class _Blob(C.Structure):
    _fields_ = [("size", W.DWORD), ("data", C.POINTER(C.c_ubyte))]


def _function(dll, name, args, result):
    function = getattr(dll, name)
    function.argtypes, function.restype = args, result
    return function


def _windows():
    if sys.platform != "win32":
        raise XhhConfigError("Managed accounts require Windows DPAPI; no plaintext fallback")
    return C.WinDLL("kernel32", use_last_error=True)


def _free(pointer):
    _function(_windows(), "LocalFree", [W.HLOCAL], W.HLOCAL)(C.cast(pointer, W.HLOCAL))


def _dpapi(data: bytes, *, decrypt=False) -> bytes:
    _windows()
    dll = C.WinDLL("crypt32", use_last_error=True)
    name = "CryptUnprotectData" if decrypt else "CryptProtectData"
    description_type = C.POINTER(W.LPWSTR) if decrypt else W.LPCWSTR
    function = _function(dll, name, [C.POINTER(_Blob), description_type, C.POINTER(_Blob),
        C.c_void_p, C.c_void_p, W.DWORD, C.POINTER(_Blob)], W.BOOL)
    buffer = C.create_string_buffer(data)
    source = _Blob(len(data), C.cast(buffer, C.POINTER(C.c_ubyte)))
    target = _Blob()
    if not function(C.byref(source), None, None, None, None, 1, C.byref(target)):
        raise XhhConfigError("Credential protection failed")
    try:
        return C.string_at(target.data, target.size)
    finally:
        _free(target.data)


def _restrict_directory(path: Path):
    # Protected owner-rights DACL is inherited by all newly created store files.
    dll = C.WinDLL("advapi32", use_last_error=True)
    convert = _function(dll, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
        [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.POINTER(W.DWORD)], W.BOOL)
    set_security = _function(dll, "SetFileSecurityW", [W.LPCWSTR, W.DWORD, C.c_void_p], W.BOOL)
    descriptor = C.c_void_p()
    if not convert("D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)", 1, C.byref(descriptor), None):
        raise XhhConfigError("Private directory protection failed")
    try:
        if not set_security(str(path), 0x80000004, descriptor):
            raise XhhConfigError("Private directory protection failed")
    finally:
        _free(descriptor)


def _alias(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", value) or value.upper() in _RESERVED:
        raise XhhConfigError("Invalid account alias")
    return value.casefold()


def _identity(value, *, optional=False):
    if optional and value in (None, ""):
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,32}", value):
        raise XhhIdentityUnboundError("Account identity must be numeric")
    return value


def _cookie(value):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 33 or ord(c) > 126 or c == ";" for c in value):
        raise XhhConfigError("Invalid account session cookie")


def _sessions(value):
    if not isinstance(value, dict) or value.keys() - _PROTOCOLS:
        raise XhhConfigError("Invalid account sessions")
    sessions = {}
    for protocol, cookie in value.items():
        if cookie is None:
            continue
        _cookie(cookie)
        sessions[protocol] = cookie
    return sessions


def _config(values):
    if not isinstance(values, dict) or values.keys() - _CONFIG_KEYS:
        raise XhhConfigError("Unsupported account configuration")
    for key, value in values.items():
        if key == "protocol_mode":
            valid = value in ("app", "web")
        elif key == "timeout":
            valid = type(value) in (float, int) and 0 < value <= 120 and math.isfinite(value)
        elif key == "signer_jar" and value is None:
            valid = True
        else:
            valid = isinstance(value, str) and bool(value.strip()) and not any(c in value for c in "\r\n\x00;")
        if not valid:
            raise XhhConfigError("Invalid account configuration value")
    return dict(values)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _verified_time(value):
    if (not isinstance(value, str)
            or not re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+00:00",
                value)):
        raise XhhConfigError("Invalid verified login evidence")
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise XhhConfigError("Invalid verified login evidence") from None
    return value


def _verified_identity(value, identity):
    if (not isinstance(value, dict)
            or set(value) != {"identity", "protocol", "method", "verified_at"}
            or value["identity"] != identity
            or not isinstance(value["method"], str)
            or value["method"] not in _LOGIN_PROTOCOLS
            or _LOGIN_PROTOCOLS[value["method"]] != value["protocol"]):
        raise XhhConfigError("Invalid verified login evidence")
    _identity(value["identity"])
    return VerifiedLoginObservation(
        **{**value, "verified_at": _verified_time(value["verified_at"])})


def _phone_observations(values, identity):
    if not isinstance(values, list) or len(values) > _MAX_LOGIN_PHONES:
        raise XhhConfigError("Invalid verified login evidence")
    observations, seen = [], set()
    for value in values:
        if (not isinstance(value, dict)
                or set(value) != {"phone", "identity", "verified_at", "source"}
                or value["source"] != "sms_login"
                or value["identity"] != identity):
            raise XhhConfigError("Invalid verified login evidence")
        _identity(value["identity"])
        phone = normalize_phone(value["phone"])
        if phone != value["phone"] or phone in seen:
            raise XhhConfigError("Invalid verified login evidence")
        seen.add(phone)
        observations.append(LoginPhoneObservation(
            phone, identity, _verified_time(value["verified_at"])))
    return observations


def _empty_login_phone():
    return {"state": "not_recorded", "masked": None, "source": None,
            "verified_at": None, "source_alias": None, "matches": None,
            "current_binding_verified": False}


def _public(account, **extra):
    identity = account.identity
    masked = identity[:2] + "*" * (len(identity) - 4) + identity[-2:] if len(identity) > 4 else "*" * len(identity)
    sessions = {protocol: {"present": protocol in account.sessions,
                           "state": "stored" if protocol in account.sessions else "missing"}
                for protocol in ("app", "web")}
    return {"alias": account.alias, "identity_masked": masked,
            "authenticated": bool(account.sessions),
            "state": "logged_in" if account.sessions else "needs_login",
            "sessions": sessions,
            "storage": "windows-dpapi", "protocol_mode": account.config.get("protocol_mode", "app"),
            "login_phone": _empty_login_phone(),
            **extra}


class AccountStore:
    def __init__(self, root: Path):
        self.root = Path(root).absolute()

    @contextmanager
    def _locked(self):
        kernel = _windows()
        create = _function(kernel, "CreateMutexW", [C.c_void_p, W.BOOL, W.LPCWSTR], W.HANDLE)
        wait = _function(kernel, "WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD)
        release = _function(kernel, "ReleaseMutex", [W.HANDLE], W.BOOL)
        close = _function(kernel, "CloseHandle", [W.HANDLE], W.BOOL)
        digest = hashlib.sha256(str(self.root.resolve()).casefold().encode()).hexdigest()
        handle = create(None, False, "Global\\xhh-sdk-accounts-" + digest)
        if not handle:
            raise XhhConfigError("Account store lock unavailable")
        acquired = False
        try:
            acquired = wait(handle, 30000) in (0, 0x80)
            if not acquired:
                raise XhhConfigError("Account store is busy")
            for path in (self.root, self.root / "accounts.json"):
                if path.exists() and path.lstat().st_file_attributes & 0x400:
                    raise XhhConfigError("Account store cannot use reparse points")
            yield
        except XhhConfigError:
            raise
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            raise XhhConfigError("Account store operation failed") from None
        finally:
            if acquired:
                release(handle)
            close(handle)

    def _read(self):
        path = self.root / "accounts.json"
        if not path.exists():
            return {}
        try:
            document = json.loads(path.read_bytes(), object_pairs_hook=_object)
            if (not isinstance(document, dict)
                    or set(document) != {"schema_version", "accounts"}
                    or type(document["schema_version"]) is not int
                    or document["schema_version"] not in (1, 2, 3)
                    or not isinstance(document["accounts"], dict)):
                raise ValueError()
            accounts = {}
            for key, sealed in document["accounts"].items():
                if not isinstance(sealed, dict) or sealed.get("protection") != "windows-dpapi":
                    raise ValueError()
                raw = _dpapi(base64.b64decode(sealed["value"], validate=True), decrypt=True)
                value = json.loads(raw, object_pairs_hook=_object)
                if not isinstance(value, dict):
                    raise ValueError()
                required = {"alias", "identity", "config", "revision"}
                if not required <= set(value):
                    raise ValueError()
                if "sessions" in value:
                    sessions = _sessions(value["sessions"])
                elif "pkey" in value:
                    mode = (value.get("config") or {}).get("protocol_mode", "app")
                    sessions = _sessions({mode: value["pkey"]}) if value["pkey"] is not None else {}
                else:
                    raise ValueError()
                account = ManagedAccount(
                    alias=value["alias"], identity=value["identity"],
                    config=value["config"], revision=value["revision"],
                    sessions=sessions, risk_token=value.get("risk_token"),
                )
                if key != _alias(account.alias) or not isinstance(account.identity, str) or not re.fullmatch(r"[0-9a-f]{32}", account.revision):
                    raise ValueError()
                _identity(account.identity, optional=not account.sessions)
                _config(account.config)
                evidence_keys = {"verified_identity", "login_phones"} & set(value)
                if evidence_keys:
                    if (document["schema_version"] != 3
                            or evidence_keys != {"verified_identity", "login_phones"}):
                        raise ValueError()
                    account.verified_identity = _verified_identity(
                        value["verified_identity"], account.identity)
                    account.login_phones = _phone_observations(
                        value["login_phones"], account.identity)
                if document["schema_version"] == 3 and set(value) - {
                        "alias", "identity", "config", "revision", "sessions", "pkey",
                        "risk_token", "verified_identity", "login_phones"}:
                    raise ValueError()
                accounts[key] = account
            return accounts
        except XhhIdentityUnboundError:
            raise
        except (XhhConfigError, OSError, ValueError, TypeError, KeyError):
            raise XhhConfigError("Account store is corrupt or unreadable; it was not reset") from None

    def _write(self, accounts):
        def payload(item):
            value = {"alias": item.alias, "identity": item.identity,
                     "sessions": item.sessions, "config": item.config,
                     "revision": item.revision, "risk_token": item.risk_token}
            if item.verified_identity is not None:
                observation = item.verified_identity
                value["verified_identity"] = {
                    "identity": observation.identity, "protocol": observation.protocol,
                    "method": observation.method, "verified_at": observation.verified_at}
                value["login_phones"] = [
                    {"phone": phone.phone, "identity": phone.identity,
                     "verified_at": phone.verified_at, "source": "sms_login"}
                    for phone in item.login_phones]
            elif item.login_phones:
                raise XhhConfigError("Invalid verified login evidence")
            return value

        version = 3 if any(item.verified_identity is not None or item.login_phones
                           for item in accounts.values()) else 2
        document = {"schema_version": version, "accounts": {key: {
            "protection": "windows-dpapi",
            "value": base64.b64encode(_dpapi(json.dumps(payload(item)).encode())).decode()}
            for key, item in accounts.items()}}
        self.root.mkdir(parents=True, exist_ok=True)
        _restrict_directory(self.root)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root, delete=False) as stream:
                name = stream.name
                json.dump(document, stream, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.root / "accounts.json")
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    def _find(self, accounts, key):
        if key not in accounts:
            raise XhhConfigError("Account not found")
        return accounts[key]

    def add(self, alias: str, *, identity: str | None = None) -> dict:
        key, identity = _alias(alias), _identity(identity, optional=True)
        with self._locked():
            accounts = self._read()
            if key in accounts:
                raise XhhConfigError("Account alias already exists")
            account = ManagedAccount(alias=alias, identity=identity, config={},
                                     revision=uuid.uuid4().hex)
            accounts[key] = account
            self._write(accounts)
            return _public(account, changed=True)

    def list(self) -> list[dict]:
        with self._locked():
            accounts = self._read()
            return [_public(a, login_phone=self._login_phone(a, accounts))
                    for _, a in sorted(accounts.items())]

    def _login_phone(self, account, accounts, *, phone=None, verified_identity=None):
        public = _empty_login_phone()
        if not (account.login_phones or account.verified_identity
                or verified_identity == account.identity and bool(account.identity)):
            return public
        distinct = {}
        for _, source in sorted(accounts.items()):
            if source.identity != account.identity:
                continue
            for observation in source.login_phones:
                metadata = {
                    "masked": observation.phone[:3] + "****" + observation.phone[-4:],
                    "source": "sms_login", "verified_at": observation.verified_at,
                    "source_alias": source.alias}
                previous = distinct.get(observation.phone)
                if previous is None or metadata["verified_at"] > previous["verified_at"]:
                    distinct[observation.phone] = metadata
        if len(distinct) > 1:
            return {**public, "state": "conflict", "source": "sms_login",
                    "candidates": list(distinct.values())}
        if distinct:
            recorded_phone, metadata = next(iter(distinct.items()))
            return {**public, **metadata, "state": "verified_at_login",
                    "matches": None if phone is None else phone == recorded_phone}
        return public

    def status(self, alias: str, *, phone: str | None = None,
               expected_revision: str | None = None,
               verified_identity: str | None = None) -> dict:
        phone = normalize_phone(phone) if phone is not None else None
        key = _alias(alias)
        with self._locked():
            accounts = self._read()
            account = self._find(accounts, key)
            if expected_revision is not None and account.revision != expected_revision:
                raise XhhConfigError("Account changed during status; retry",
                                     code="account_changed")
            if verified_identity is not None:
                if expected_revision is None or _identity(verified_identity) != account.identity:
                    raise XhhConfigError("Invalid status identity verification")
            return _public(
                account,
                login_phone=self._login_phone(
                    account, accounts, phone=phone, verified_identity=verified_identity),
                session_valid="not_checked", api_identity_verified=False,
                risk_token_present=bool(account.risk_token))

    def get(self, alias: str) -> ManagedAccount:
        key = _alias(alias)
        with self._locked():
            return self._find(self._read(), key)

    def _change(self, alias, change):
        key = _alias(alias)
        with self._locked():
            accounts = self._read()
            account = self._find(accounts, key)
            change(account)
            account.revision = uuid.uuid4().hex
            self._write(accounts)
            return _public(account, changed=True,
                           login_phone=self._login_phone(account, accounts))

    def configure(self, alias: str, values: dict) -> dict:
        values = _config(values)
        return self._change(alias, lambda account: account.config.update(values))

    def save_login(self, alias: str, *, pkey: str, identity: str, expected_revision: str,
                   protocol_mode: str = "app",
                   observation: VerifiedLoginObservation | None = None) -> dict:
        _cookie(pkey)
        identity = _identity(identity)
        if protocol_mode not in _PROTOCOLS:
            raise XhhConfigError("protocol_mode must be app or web")
        if observation is not None:
            if not isinstance(observation, VerifiedLoginObservation):
                raise XhhConfigError("Invalid verified login evidence")
            attestation = _verified_identity({
                "identity": observation.identity, "protocol": observation.protocol,
                "method": observation.method, "verified_at": observation.verified_at},
                identity)
            if observation.protocol != protocol_mode:
                raise XhhConfigError("Invalid verified login evidence")
            phone = normalize_phone(observation.phone) if observation.method == "sms" else None
            if observation.method != "sms" and observation.phone is not None:
                raise XhhConfigError("Invalid verified login evidence")
        def update(account):
            if account.revision != expected_revision:
                raise XhhConfigError("Account changed during login; start login again")
            if account.identity and account.identity != identity:
                raise XhhConfigError("Login identity does not match the bound account")
            account.identity = identity
            account.sessions[protocol_mode] = pkey
            if observation is not None:
                if phone is not None:
                    previous = next(
                        (item for item in account.login_phones if item.phone == phone), None)
                    if previous is None and len(account.login_phones) >= _MAX_LOGIN_PHONES:
                        raise XhhConfigError("Verified login phone history is full")
                    if previous is not None:
                        account.login_phones.remove(previous)
                    account.login_phones.append(LoginPhoneObservation(
                        phone, identity, attestation.verified_at))
                account.verified_identity = attestation
        return self._change(alias, update)

    def logout(self, alias: str, *, protocol: str | None = None) -> dict:
        def update(account):
            selected = account.select_session(protocol).protocol
            account.sessions.pop(selected, None)
        return self._change(alias, update)

    def logout_all(self, alias: str) -> dict:
        return self._change(alias, lambda account: account.sessions.clear())

    def set_risk_token(self, alias: str, token: str) -> dict:
        """Store the device anti-fraud token (sent as the x_xhh_tokenid cookie)."""
        if not isinstance(token, str) or not token.strip() or any(c in token for c in "\r\n\x00;"):
            raise XhhConfigError("Invalid risk token")
        return self._change(alias, lambda account: setattr(account, "risk_token", token.strip()))

    def remove(self, alias: str) -> dict:
        key = _alias(alias)
        with self._locked():
            accounts = self._read()
            account = self._find(accounts, key)
            del accounts[key]
            self._write(accounts)
            return {"alias": account.alias, "changed": True, "removed": True}

    def get_config(self, alias: str, *, protocol: str | None = None) -> XhhConfig:
        account = self.get(alias)
        selection = account.select_session(protocol)
        selected = selection.protocol
        pkey = account.sessions[selected]
        if not pkey:
            raise _missing_session(account, selected)
        _identity(account.identity)
        try:
            config = XhhConfig(pkey=pkey, heybox_id=account.identity,
                               risk_token=account.risk_token,
                               **{**account.config, "protocol_mode": selected},
                               extra={"session_revision": account.revision})
            config.validate()
            return config
        except XhhIdentityUnboundError:
            raise
        except (XhhConfigError, TypeError, ValueError):
            raise XhhConfigError("Invalid managed account configuration") from None
