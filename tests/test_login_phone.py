from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(os.environ.get("XHH_TEST_SKILL_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "scripts"))

import xhh
from xhh_sdk import accounts, cli, login
from xhh_sdk.config import XhhConfig
from xhh_sdk.exceptions import XhhAuthError, XhhConfigError
from xhh_sdk.secure_phone import normalize_phone


class LoginPhoneTests(unittest.TestCase):
    def setUp(self):
        self.context = contextlib.ExitStack()
        self.addCleanup(self.context.close)
        self.root = Path(self.context.enter_context(tempfile.TemporaryDirectory()))
        self.context.enter_context(patch.object(
            accounts, "_dpapi", side_effect=lambda value, decrypt=False: value))
        self.context.enter_context(patch.object(accounts, "_restrict_directory", return_value=None))
        self.context.enter_context(patch.object(
            accounts.AccountStore, "_locked", new=lambda self: contextlib.nullcontext()))
        self.store = accounts.AccountStore(self.root)
        self.store.add("fixture", identity="101")
        self.store.set_risk_token("fixture", "fixture-device")

    def invoke(self, *arguments):
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = xhh.main(["--data-dir", str(self.root), *arguments])
        return code, json.loads(output.getvalue()), output.getvalue() + errors.getvalue()

    @property
    def account_file(self):
        return self.root / "accounts.json"

    def save_verified(self, alias="fixture", phone="13500000000",
                      identity="101", method="sms"):
        protocol = "app" if method == "sms" else "web"
        account = self.store.get(alias)
        return self.store.save_login(
            alias, pkey="fixture-session", identity=identity,
            expected_revision=account.revision, protocol_mode=protocol,
            observation=accounts.VerifiedLoginObservation(
                identity=identity, protocol=protocol, method=method,
                phone=phone if method == "sms" else None,
                verified_at="2026-11-17T00:00:00+00:00"))

    def fixture_document(self):
        document = json.loads(self.account_file.read_bytes())
        value = json.loads(base64.b64decode(document["accounts"]["fixture"]["value"]))
        return document, value

    def write_fixture_document(self, document, value):
        document["accounts"]["fixture"]["value"] = base64.b64encode(
            json.dumps(value).encode()).decode()
        self.account_file.write_text(json.dumps(document), encoding="utf-8")

    def test_successful_sms_login_retains_masked_phone_for_uid(self):
        result = SimpleNamespace(pkey="fixture-session", identity="101")
        client = SimpleNamespace(account_info=lambda: {"user": {"heybox_id": "101"}})
        with patch.object(login, "login_sms_verify", return_value=result), \
                patch.object(XhhConfig, "validate", return_value=None), \
                patch.object(cli, "XhhClient", return_value=client):
            code, report, output = self.invoke(
                "account", "login", "fixture", "--method", "sms",
                "--phone", "13500000000", "--code", "123456", "--confirm")

        self.assertEqual(code, 0, report)
        self.assertTrue(report["api_identity_verified"])
        self.assertTrue(report["candidate_persisted"])
        _, listed, list_output = self.invoke("account", "list")
        phone = listed["accounts"][0].get("login_phone")
        self.assertIsNotNone(phone, "successful SMS login dropped the phone-to-UID evidence")
        self.assertEqual(phone["state"], "verified_at_login")
        self.assertEqual(phone["masked"], "135****0000")
        self.assertEqual(phone["source"], "sms_login")
        self.assertEqual(phone["source_alias"], "fixture")
        self.assertIsInstance(phone["verified_at"], str)
        self.assertIsNone(phone["matches"])
        self.assertFalse(phone["current_binding_verified"])
        self.assertEqual(report["login_phone"], phone)
        self.assertNotIn("13500000000", output + list_output)
        self.assertNotIn("fixture-session", output + list_output)

    def test_profile_uid_must_agree_before_recording_login_evidence(self):
        result = login.LoginResult(pkey="fixture-session", identity="101")
        for data, expected in (
                ({"profile": {"heybox_id": "101"}}, 0),
                ({"profile": {"heybox_id": "202"},
                  "account_detail": {"userid": "101"}}, 1)):
            with self.subTest(data=data):
                before = self.account_file.read_bytes()
                with patch.object(login, "login_sms_verify", return_value=result), \
                        patch.object(XhhConfig, "validate", return_value=None), \
                        patch.object(cli, "XhhClient") as client:
                    client.return_value.account_info.return_value = data
                    code, report, _ = self.invoke(
                        "account", "login", "fixture", "--method", "sms",
                        "--phone", "13500000000", "--code", "123456", "--confirm")
                self.assertEqual(code, expected, report)
                if expected:
                    self.assertFalse(report["api_identity_verified"])
                    self.assertEqual(self.account_file.read_bytes(), before)

    def test_code_request_does_not_create_evidence_or_replace_store(self):
        before = self.account_file.read_bytes()
        with patch.object(login, "login_sms_request", return_value={"status": "ok"}) as request, \
                patch.object(cli, "XhhClient") as client:
            code, report, output = self.invoke(
                "account", "login", "fixture", "--method", "sms",
                "--phone", "+86 13500000000", "--confirm")
        self.assertEqual(code, 0, report)
        self.assertTrue(report["code_sent"])
        self.assertEqual(request.call_args.args[0], "13500000000")
        client.assert_not_called()
        self.assertEqual(self.account_file.read_bytes(), before)
        self.assertEqual(self.store.status("fixture")["login_phone"]["state"], "not_recorded")
        self.assertNotIn("13500000000", output)

    def test_failed_exchange_and_failed_uid_checks_leave_old_bytes(self):
        cases = (
            ("exchange", None),
            ("login_identity", {"heybox_id": "101"}),
            ("online_identity", {"heybox_id": "202"}),
            ("online_rejected", None),
        )
        for case, data in cases:
            with self.subTest(case=case):
                before = self.account_file.read_bytes()
                result = login.LoginResult(
                    pkey="fixture-session", identity="202" if case == "login_identity" else "101")
                with patch.object(login, "login_sms_verify", return_value=result) as exchange, \
                        patch.object(XhhConfig, "validate", return_value=None), \
                        patch.object(cli, "XhhClient") as client:
                    if case == "exchange":
                        exchange.side_effect = XhhConfigError(
                            "refused 13500000000 with code 123456")
                    elif case == "online_rejected":
                        client.return_value.account_info.side_effect = XhhAuthError("expired")
                    else:
                        client.return_value.account_info.return_value = data
                    code, report, output = self.invoke(
                        "account", "login", "fixture", "--method", "sms",
                        "--phone", "13500000000", "--code", "123456", "--confirm")
                self.assertEqual(code, 1, report)
                self.assertEqual(self.account_file.read_bytes(), before)
                self.assertEqual(
                    self.store.status("fixture")["login_phone"]["state"], "not_recorded")
                for secret in ("13500000000", "123456", "fixture-session"):
                    self.assertNotIn(secret, output)

    def test_local_exact_comparison_match_mismatch_and_unknown(self):
        with patch.object(cli, "XhhClient") as client, \
                patch.object(login, "login_sms_request") as request, \
                patch.object(login, "login_sms_verify") as exchange:
            code, report, output = self.invoke(
                "account", "status", "fixture", "--phone", "13500000000")
            self.assertEqual(code, 1)
            self.assertEqual(report["login_phone"]["state"], "not_recorded")
            self.assertIsNone(report["login_phone"]["matches"])
            self.save_verified()
            before = self.account_file.read_bytes()
            for supplied, expected in (
                    ("13500000000", True), (" +86 13500000000 ", True),
                    ("008613500000000", True), ("13511110000", False)):
                with self.subTest(supplied=supplied):
                    code, report, output = self.invoke(
                        "account", "status", "fixture", "--phone", supplied)
                    self.assertEqual(code, 0 if expected else 1)
                    self.assertIs(report["login_phone"]["matches"], expected)
                    self.assertFalse(report["login_phone"]["current_binding_verified"])
                    self.assertEqual(report["session_valid"], "not_checked")
                    self.assertNotIn(supplied.strip(), output)
                    self.assertNotIn(normalize_phone(supplied), output)
            client.assert_not_called()
            request.assert_not_called()
            exchange.assert_not_called()
        self.assertEqual(self.account_file.read_bytes(), before)

    def test_invalid_phone_fails_before_any_request_without_echo(self):
        for supplied in ("not-a-phone", "13500000000-extra", "１３５００００００００", ""):
            for action in ("status", "login"):
                with self.subTest(action=action, supplied=supplied):
                    before = self.account_file.read_bytes()
                    with patch.object(cli, "XhhClient") as client, \
                            patch.object(login, "login_sms_request") as request, \
                            patch.object(login, "login_sms_verify") as exchange:
                        arguments = (
                            ["account", "status", "fixture", "--online", "--phone", supplied]
                            if action == "status" else
                            ["account", "login", "fixture", "--method", "sms",
                             "--phone", supplied, "--code", "123456", "--confirm"])
                        code, report, output = self.invoke(*arguments)
                    self.assertEqual(code, 1)
                    self.assertFalse(report["ok"])
                    self.assertEqual(
                        report["error"]["message"],
                        "phone must be a supported mainland China mobile number")
                    if supplied:
                        self.assertNotIn(supplied, output)
                    client.assert_not_called()
                    request.assert_not_called()
                    exchange.assert_not_called()
                    self.assertEqual(self.account_file.read_bytes(), before)

    def test_sms_and_comparison_share_normalization_and_fixed_errors(self):
        for supplied in ("13500000000", "+8613500000000", "0086 13500000000"):
            self.assertEqual(login._validate_phone(supplied), normalize_phone(supplied))
        with patch.object(login, "_signed_post") as post:
            for operation in (
                    lambda: login.login_sms_request("13500000000-invalid"),
                    lambda: login.login_sms_verify("13500000000-invalid", "123456")):
                with self.assertRaises(XhhConfigError) as failure:
                    operation()
                self.assertNotIn("13500000000", str(failure.exception))
            post.assert_not_called()
        with self.assertRaises(XhhConfigError) as failure:
            login._require_ok({
                "status": "refused-13500000000", "msg": "phone 13500000000 code 123456"})
        self.assertNotIn("13500000000", str(failure.exception))
        self.assertNotIn("123456", str(failure.exception))

    def test_legacy_v1_and_v2_stay_unknown_and_preserve_sessions(self):
        for version in (1, 2):
            with self.subTest(version=version):
                document, value = self.fixture_document()
                value.pop("verified_identity", None)
                value.pop("login_phones", None)
                value.pop("pkey", None)
                if version == 1:
                    value.pop("sessions", None)
                    value["pkey"] = "fixture-session"
                else:
                    value["sessions"] = {"app": "fixture-session"}
                document["schema_version"] = version
                self.write_fixture_document(document, value)
                before = self.account_file.read_bytes()
                code, report, _ = self.invoke(
                    "account", "status", "fixture", "--phone", "13500000000")
                self.assertEqual(code, 1)
                self.assertEqual(report["login_phone"]["state"], "not_recorded")
                self.assertTrue(report["sessions"]["app"]["present"])
                self.assertEqual(self.account_file.read_bytes(), before)
                current = self.store.get("fixture")
                self.store.save_login(
                    "fixture", pkey="fixture-new-session", identity="101",
                    expected_revision=current.revision, protocol_mode="web")
                self.store.configure("fixture", {"device_info": "fixture-device"})
                self.assertEqual(self.store.get("fixture").sessions, {
                    "app": "fixture-session", "web": "fixture-new-session"})
                rewritten, _ = self.fixture_document()
                self.assertEqual(rewritten["schema_version"], 2)
                self.assertEqual(
                    self.store.status("fixture")["login_phone"]["state"], "not_recorded")

    def test_non_sms_configuration_and_logout_preserve_history(self):
        self.save_verified()
        expected = self.store.status("fixture", phone="13500000000")["login_phone"]
        account = self.store.get("fixture")
        self.store.save_login(
            "fixture", pkey="fixture-web-session", identity="101",
            expected_revision=account.revision, protocol_mode="web")
        self.save_verified(method="creator")
        self.store.configure("fixture", {"protocol_mode": "web", "device_info": "fixture-device"})
        self.store.set_risk_token("fixture", "fixture-new-device-token")
        self.store.logout("fixture", protocol="web")
        self.assertEqual(
            self.store.status("fixture", phone="13500000000")["login_phone"], expected)
        self.store.logout_all("fixture")
        report = self.store.status("fixture", phone="13500000000")
        self.assertEqual(report["login_phone"], expected)
        self.assertFalse(report["authenticated"])
        self.assertEqual(report["state"], "needs_login")
        document, value = self.fixture_document()
        self.assertEqual(document["schema_version"], 3)
        self.assertEqual(value["verified_identity"]["method"], "creator")

    def test_unverified_save_login_cannot_introduce_phone_evidence(self):
        account = self.store.get("fixture")
        self.store.save_login(
            "fixture", pkey="fixture-session", identity="101",
            expected_revision=account.revision)
        self.assertEqual(self.store.status("fixture")["login_phone"]["state"], "not_recorded")
        self.assertEqual(self.fixture_document()[0]["schema_version"], 2)
        before = self.account_file.read_bytes()
        account = self.store.get("fixture")
        for invalid in (
                {"phone": "13500000000"},
                accounts.VerifiedLoginObservation(
                    identity="101", protocol="web", method="sms", phone="13500000000"),
                accounts.VerifiedLoginObservation(
                    identity="202", protocol="app", method="sms", phone="13500000000"),
                accounts.VerifiedLoginObservation(
                    identity="101", protocol="app", method="sms", phone=None),
                accounts.VerifiedLoginObservation(
                    identity="101", protocol="app", method="sms",
                    phone="13500000000", verified_at="invalid")):
            with self.subTest(observation=type(invalid).__name__):
                with self.assertRaises(XhhConfigError):
                    self.store.save_login(
                        "fixture", pkey="fixture-new-session", identity="101",
                        expected_revision=account.revision, observation=invalid)
                self.assertEqual(self.account_file.read_bytes(), before)

    def test_same_uid_web_inheritance_requires_actual_verification(self):
        self.save_verified()
        self.store.add("web", identity="101")
        account = self.store.get("web")
        self.store.save_login(
            "web", pkey="fixture-web-session", identity="101",
            expected_revision=account.revision, protocol_mode="web")
        self.assertEqual(self.store.status("web")["login_phone"]["state"], "not_recorded")
        before = self.account_file.read_bytes()
        with patch.object(XhhConfig, "validate", return_value=None), \
                patch.object(cli, "XhhClient") as client:
            client.return_value.account_info.return_value = {"user": {"heybox_id": "101"}}
            code, report, output = self.invoke(
                "account", "status", "web", "--phone", "13500000000", "--online")
        self.assertEqual(code, 0, report)
        self.assertTrue(report["api_identity_verified"])
        self.assertTrue(report["login_phone"]["matches"])
        self.assertEqual(report["login_phone"]["source_alias"], "fixture")
        self.assertNotIn("13500000000", output)
        self.assertEqual(self.account_file.read_bytes(), before)
        self.assertEqual(self.store.status("web")["login_phone"]["state"], "not_recorded")
        self.save_verified(alias="web", method="creator")
        code, report, _ = self.invoke(
            "account", "status", "web", "--phone", "13500000000")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["login_phone"]["source_alias"], "fixture")

    def test_creator_cli_login_attests_identity_without_owning_sms_evidence(self):
        self.save_verified()
        self.store.add("web", identity="101")
        with patch.object(
                login, "login_creator",
                return_value=login.LoginResult(pkey="fixture-web-session", identity="101")), \
                patch.object(XhhConfig, "validate", return_value=None), \
                patch.object(cli, "XhhClient") as client:
            client.return_value.account_info.return_value = {"heybox_id": "101"}
            code, report, output = self.invoke(
                "account", "login", "web", "--method", "creator", "--confirm")
        self.assertEqual(code, 0, report)
        self.assertTrue(report["candidate_persisted"])
        self.assertEqual(report["login_phone"]["source_alias"], "fixture")
        self.assertNotIn("fixture-web-session", output)
        self.store.remove("fixture")
        self.assertEqual(self.store.status("web")["login_phone"]["state"], "not_recorded")

    def test_verified_other_uid_cannot_inherit_phone(self):
        self.save_verified()
        self.store.add("other", identity="202")
        self.save_verified(alias="other", identity="202", method="creator")
        before = self.account_file.read_bytes()
        with patch.object(XhhConfig, "validate", return_value=None), \
                patch.object(cli, "XhhClient") as client:
            client.return_value.account_info.return_value = {"heybox_id": "202"}
            code, report, _ = self.invoke(
                "account", "status", "other", "--phone", "13500000000", "--online")
        self.assertEqual(code, 1, report)
        self.assertTrue(report["api_identity_verified"])
        self.assertEqual(report["login_phone"]["state"], "not_recorded")
        self.assertIsNone(report["login_phone"]["matches"])
        self.assertEqual(self.account_file.read_bytes(), before)

    def test_online_comparison_requires_both_phone_match_and_uid_success(self):
        self.save_verified()
        for phone, data, expected in (
                ("13500000000", {"heybox_id": "101"}, 0),
                ("13511110000", {"heybox_id": "101"}, 1),
                ("13500000000", {"heybox_id": "202"}, 1)):
            with self.subTest(phone=phone, data=data):
                before = self.account_file.read_bytes()
                with patch.object(XhhConfig, "validate", return_value=None), \
                        patch.object(cli, "XhhClient") as client:
                    client.return_value.account_info.return_value = data
                    code, report, _ = self.invoke(
                        "account", "status", "fixture", "--phone", phone, "--online")
                self.assertEqual(code, expected, report)
                self.assertEqual(self.account_file.read_bytes(), before)

    def test_equal_normalized_numbers_dedupe_but_equal_masks_conflict(self):
        self.save_verified()
        self.save_verified(phone="+86 13500000000")
        self.store.add("second", identity="101")
        self.save_verified(alias="second", phone="008613500000000")
        report = self.store.status("fixture", phone="13500000000")
        self.assertTrue(report["login_phone"]["matches"])
        self.assertEqual(report["login_phone"]["state"], "verified_at_login")
        self.save_verified(alias="second", phone="13511110000")
        for phone in ("13500000000", "13511110000"):
            code, report, output = self.invoke(
                "account", "status", "fixture", "--phone", phone)
            self.assertEqual(code, 1)
            evidence = report["login_phone"]
            self.assertEqual(evidence["state"], "conflict")
            self.assertIsNone(evidence["matches"])
            self.assertEqual(len(evidence["candidates"]), 2)
            self.assertEqual(
                {frozenset(candidate) for candidate in evidence["candidates"]},
                {frozenset({"masked", "source", "verified_at", "source_alias"})})
            self.assertEqual(
                {candidate["masked"] for candidate in evidence["candidates"]},
                {"135****0000"})
            self.assertNotIn("13500000000", output)
            self.assertNotIn("13511110000", output)
        self.store.remove("second")
        self.assertTrue(self.store.status("fixture", phone="13500000000")["login_phone"]["matches"])

    def test_history_bound_rejects_overflow_without_losing_existing_evidence(self):
        for suffix in range(16):
            self.save_verified(phone=f"1350000{suffix:04d}")
        before = self.account_file.read_bytes()
        with self.assertRaises(XhhConfigError):
            self.save_verified(phone="13511110000")
        self.assertEqual(self.account_file.read_bytes(), before)
        evidence = self.store.status("fixture")["login_phone"]
        self.assertEqual(evidence["state"], "conflict")
        self.assertEqual(len(evidence["candidates"]), 16)
        self.save_verified(phone="+86 13500000000")
        self.assertEqual(len(self.store.status("fixture")["login_phone"]["candidates"]), 16)

    def test_stale_revision_and_bound_identity_leave_old_bytes(self):
        account = self.store.get("fixture")
        observation = accounts.VerifiedLoginObservation(
            identity="101", protocol="app", method="sms", phone="13500000000")
        self.store.configure("fixture", {"device_info": "fixture-new-device"})
        before = self.account_file.read_bytes()
        with self.assertRaises(XhhConfigError):
            self.store.save_login(
                "fixture", pkey="fixture-session", identity="101",
                expected_revision=account.revision, observation=observation)
        self.assertEqual(self.account_file.read_bytes(), before)
        account = self.store.get("fixture")
        with self.assertRaises(XhhConfigError):
            self.store.save_login(
                "fixture", pkey="fixture-session", identity="202",
                expected_revision=account.revision,
                observation=accounts.VerifiedLoginObservation(
                    identity="202", protocol="app", method="sms", phone="13500000000"))
        self.assertEqual(self.account_file.read_bytes(), before)
        with self.assertRaises(XhhConfigError):
            self.store.status(
                "fixture", phone="13500000000", verified_identity="101")
        with self.assertRaises(XhhConfigError):
            self.store.status(
                "fixture", phone="13500000000", expected_revision=account.revision,
                verified_identity="202")
        self.assertEqual(self.account_file.read_bytes(), before)

    def test_login_revision_race_does_not_persist_candidate_or_evidence(self):
        changed = []

        def verify():
            self.store.configure("fixture", {"device_info": "fixture-new-device"})
            changed.append(self.account_file.read_bytes())
            return {"heybox_id": "101"}

        with patch.object(
                login, "login_sms_verify",
                return_value=login.LoginResult(pkey="fixture-session", identity="101")), \
                patch.object(XhhConfig, "validate", return_value=None), \
                patch.object(cli, "XhhClient") as client:
            client.return_value.account_info.side_effect = verify
            code, report, output = self.invoke(
                "account", "login", "fixture", "--method", "sms",
                "--phone", "13500000000", "--code", "123456", "--confirm")
        self.assertEqual(code, 1, report)
        self.assertEqual(self.account_file.read_bytes(), changed[0])
        self.assertEqual(self.store.status("fixture")["login_phone"]["state"], "not_recorded")
        self.assertFalse(self.store.get("fixture").sessions)
        self.assertNotIn("13500000000", output)

    def test_online_revision_race_cannot_grant_transient_inheritance(self):
        self.save_verified()
        self.store.add("web", identity="101")
        account = self.store.get("web")
        self.store.save_login(
            "web", pkey="fixture-web-session", identity="101",
            expected_revision=account.revision, protocol_mode="web")
        changed = []

        def verify():
            self.store.configure("web", {"device_info": "fixture-new-device"})
            changed.append(self.account_file.read_bytes())
            return {"heybox_id": "101"}

        with patch.object(XhhConfig, "validate", return_value=None), \
                patch.object(cli, "XhhClient") as client:
            client.return_value.account_info.side_effect = verify
            code, report, _ = self.invoke(
                "account", "status", "web", "--phone", "13500000000", "--online")
        self.assertEqual(code, 1)
        self.assertEqual(report["state"], "account_changed")
        self.assertEqual(report["login_phone"]["state"], "not_recorded")
        self.assertEqual(self.account_file.read_bytes(), changed[0])
        self.assertEqual(self.store.status("web")["login_phone"]["state"], "not_recorded")

    def test_pre_replace_failures_preserve_session_and_evidence_atomically(self):
        self.save_verified()

        def fail_encryption(value, decrypt=False):
            if decrypt:
                return value
            raise XhhConfigError("fixture protection failure")

        failures = (
            patch.object(accounts, "_dpapi", side_effect=fail_encryption),
            patch.object(accounts, "_restrict_directory", side_effect=OSError("fixture ACL failure")),
            patch.object(accounts.os, "fsync", side_effect=OSError("fixture flush failure")),
            patch.object(accounts.os, "replace", side_effect=OSError("fixture replace failure")),
        )
        for failure in failures:
            with self.subTest(failure=failure.attribute):
                before = self.account_file.read_bytes()
                with failure, self.assertRaises((XhhConfigError, OSError)):
                    self.save_verified(phone="13511110000")
                self.assertEqual(self.account_file.read_bytes(), before)
                evidence = self.store.status("fixture", phone="13500000000")["login_phone"]
                self.assertTrue(evidence["matches"])
                self.assertEqual(evidence["state"], "verified_at_login")
                self.assertEqual(self.store.get("fixture").sessions["app"], "fixture-session")

    def test_corrupt_or_unsupported_evidence_rejects_without_reset(self):
        self.save_verified()
        original = self.account_file.read_bytes()
        for corruption in (
                "phone", "source", "identity", "timestamp", "duplicate",
                "attestation", "missing", "extra", "legacy", "schema"):
            with self.subTest(corruption=corruption):
                self.account_file.write_bytes(original)
                document, value = self.fixture_document()
                if corruption == "phone":
                    value["login_phones"][0]["phone"] = "13500000000-invalid"
                elif corruption == "source":
                    value["login_phones"][0]["source"] = "configured_phone"
                elif corruption == "identity":
                    value["login_phones"][0]["identity"] = "202"
                elif corruption == "timestamp":
                    value["login_phones"][0]["verified_at"] = "2026-02-30T00:00:00+00:00"
                elif corruption == "duplicate":
                    value["login_phones"].append(dict(value["login_phones"][0]))
                elif corruption == "attestation":
                    value["verified_identity"]["method"] = "unsupported"
                    value["verified_identity"]["protocol"] = None
                elif corruption == "missing":
                    value.pop("verified_identity")
                elif corruption == "extra":
                    value["login_phones"][0]["verifier"] = "fixture-verifier"
                elif corruption == "legacy":
                    document["schema_version"] = 2
                else:
                    document["schema_version"] = 4
                self.write_fixture_document(document, value)
                corrupt = self.account_file.read_bytes()
                with self.assertRaises(XhhConfigError) as failure:
                    self.store.list()
                self.assertIn("not reset", str(failure.exception))
                self.assertNotIn("13500000000", str(failure.exception))
                with self.assertRaises(XhhConfigError):
                    self.store.configure("fixture", {"device_info": "fixture-device"})
                self.assertEqual(self.account_file.read_bytes(), corrupt)
        self.account_file.write_bytes(original)

    def test_secrets_are_absent_from_repr_public_outputs_and_config_metadata(self):
        observation = accounts.VerifiedLoginObservation(
            identity="101", protocol="app", method="sms", phone="13500000000")
        self.save_verified()
        account = self.store.get("fixture")
        representations = (
            repr(observation), repr(account), repr(account.login_phones[0]),
            repr(login.LoginResult(pkey="fixture-session", identity="101")))
        for representation in representations:
            for secret in ("13500000000", "fixture-session", "fixture-device"):
                self.assertNotIn(secret, representation)
        with patch.object(XhhConfig, "validate", return_value=None):
            config = self.store.get_config("fixture")
        self.assertNotIn("13500000000", repr(config))
        self.assertEqual(set(config.extra), {"session_revision"})
        for arguments in (
                ("account", "list"),
                ("account", "status", "fixture"),
                ("account", "status", "fixture", "--phone", "13500000000")):
            _, _, output = self.invoke(*arguments)
            self.assertNotIn("13500000000", output)
            self.assertNotIn("fixture-session", output)
        document, value = self.fixture_document()
        self.assertNotIn("13500000000", json.dumps(document))
        self.assertEqual(value["login_phones"][0]["phone"], "13500000000")
        self.assertNotIn("phone", value["config"])

    def test_key_redaction_hides_raw_phone_and_sms_secrets_but_keeps_public_projection(self):
        public = {
            "state": "verified_at_login", "masked": "135****0000",
            "source": "sms_login", "verified_at": "2026-11-17T00:00:00+00:00",
            "source_alias": "fixture", "matches": True, "current_binding_verified": False}
        data = {
            "phone": "13500000000", "phone_num": 13500000000,
            "mobile": ["13500000000"], "msisdn": {"value": "13500000000"},
            "sms_code": "123456", "code": "123456", "verifier": "fixture-verifier",
            "nested_sms_code": {"value": "123456"},
            "login_phone": public, "error": {"code": "account_changed"}}
        redacted = cli._redact(data)
        for key in ("phone", "phone_num", "mobile", "msisdn", "sms_code", "code", "verifier"):
            self.assertEqual(redacted[key], "[REDACTED]")
        self.assertEqual(redacted["nested_sms_code"], "[REDACTED]")
        self.assertEqual(cli._redact({"login_phone": ["13500000000"]}),
                         {"login_phone": "[REDACTED]"})
        self.assertEqual(redacted["login_phone"], public)
        self.assertEqual(redacted["error"]["code"], "account_changed")
        self.assertNotIn("13500000000", json.dumps(redacted))
        self.assertNotIn("123456", json.dumps(redacted))

    def test_schema_three_blocks_old_reader_rewrite_before_evidence_can_be_erased(self):
        self.save_verified()
        before = self.account_file.read_bytes()

        def old_reader_rewrite(path):
            document = json.loads(path.read_bytes())
            if (not isinstance(document, dict)
                    or set(document) != {"schema_version", "accounts"}
                    or type(document["schema_version"]) is not int
                    or document["schema_version"] not in (1, 2)
                    or not isinstance(document["accounts"], dict)):
                raise ValueError("old account reader rejected schema")
            for sealed in document["accounts"].values():
                value = json.loads(base64.b64decode(sealed["value"]))
                rewritten = {key: value.get(key) for key in (
                    "alias", "identity", "sessions", "config", "revision", "risk_token")}
                sealed["value"] = base64.b64encode(json.dumps(rewritten).encode()).decode()
            document["schema_version"] = 2
            path.write_text(json.dumps(document), encoding="utf-8")

        self.assertEqual(self.fixture_document()[0]["schema_version"], 3)
        with self.assertRaises(ValueError):
            old_reader_rewrite(self.account_file)
        self.assertEqual(self.account_file.read_bytes(), before)
        self.assertTrue(self.store.status("fixture", phone="13500000000")["login_phone"]["matches"])


if __name__ == "__main__":
    unittest.main()
