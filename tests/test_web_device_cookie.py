from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(os.environ.get("XHH_TEST_SKILL_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "scripts"))

from xhh_sdk.accounts import AccountStore, ManagedAccount
from xhh_sdk.config import XhhConfig
from xhh_sdk.exceptions import XhhConfigError
from xhh_sdk.transport import Transport
from xhh_sdk.cli import _redact
from xhh import redact


class WebDeviceCookieTests(unittest.TestCase):
    def test_managed_device_cookie_reaches_its_selected_web_session(self):
        account = ManagedAccount(alias="fixture", identity="101", sessions={"web": "fixture-session-only"},
                                 config={"protocol_mode": "web"}, revision="fixture-revision",
                                 risk_token="fixture-device-only")
        with tempfile.TemporaryDirectory() as folder:
            store = AccountStore(Path(folder) / "unused")
            with patch.object(store, "get", return_value=account) as selected:
                config = store.get_config("fixture")
            selected.assert_called_once_with("fixture")
            self.assertEqual(config.heybox_id, "101")
            self.assertIn("x_xhh_tokenid=fixture-device-only", config.cookie_web)
            self.assertFalse((Path(folder) / "unused").exists())

    def test_web_request_keeps_bound_device_cookie_and_refuses_override(self):
        config = XhhConfig(pkey="fixture-session-only", heybox_id="101", protocol_mode="web",
                           risk_token="fixture-device-only")
        transport = Transport(config)
        with patch.object(transport, "_open", return_value=b'{"status":"ok","result":{}}') as opened:
            transport.web_signed_request("/account/restore_login")
            request = opened.call_args.args[0]
            self.assertIn("x_xhh_tokenid=fixture-device-only", request.get_header("Cookie"))
            with self.assertRaises(XhhConfigError):
                transport.web_signed_request("/account/restore_login",
                                             extra_cookies={"x_xhh_tokenid": "another-device"})
            self.assertEqual(opened.call_count, 1)

    def test_device_cookie_rejects_header_delimiters_and_wrong_types(self):
        for value in (True, "", "has space", "cookie;other=value", "line\r\nbreak", "非ASCII"):
            with self.subTest(value=value), self.assertRaises(XhhConfigError):
                XhhConfig(pkey="fixture-session-only", heybox_id="101", protocol_mode="web",
                          risk_token=value).validate()

    def test_no_device_cookie_and_app_cookie_preserve_existing_headers(self):
        config = XhhConfig(pkey="fixture-session-only", heybox_id="101", protocol_mode="web")
        self.assertNotIn("x_xhh_tokenid", config.cookie_web)
        config.risk_token = "fixture-device-only"
        self.assertNotIn("x_xhh_tokenid", config.cookie_app)

    def test_cli_errors_redact_session_and_device_credentials(self):
        error = {"message": "rejected fixture-session-only and fixture-device-only"}
        for redactor in (redact, _redact):
            result = redactor(error, ("fixture-session-only", "fixture-device-only"))
            self.assertEqual(result["message"], "rejected [REDACTED] and [REDACTED]")


if __name__ == "__main__":
    unittest.main()
