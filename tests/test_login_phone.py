from __future__ import annotations

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
        with contextlib.redirect_stdout(output):
            code = xhh.main(["--data-dir", str(self.root), *arguments])
        return code, json.loads(output.getvalue()), output.getvalue()

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
        self.assertFalse(phone["current_binding_verified"])
        self.assertNotIn("13500000000", output + list_output)
        self.assertNotIn("fixture-session", output + list_output)


if __name__ == "__main__":
    unittest.main()
