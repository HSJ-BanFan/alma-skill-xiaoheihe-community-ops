import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import xhh
from browser import Browser
from xhh_sdk import accounts
from xhh_sdk.config import XhhConfig
from xhh_sdk.exceptions import XhhCaptchaError, XhhSignerError
from xhh_sdk.transport import Transport


class ClientFixture:
    def __init__(self, protocol, handler):
        self.config = XhhConfig(pkey=protocol + "-pkey", heybox_id="101",
                                protocol_mode=protocol)
        self.transport = self
        self.handler = handler
        self.calls = []

    def signed_request(self, path, query=None):
        self.calls.append((path, query))
        return self.handler(path, query, self.config.protocol_mode)

    def _open(self, request):
        query = {
            key: values[-1] for key, values in
            parse_qs(urlsplit(request.full_url).query).items()
        }
        self.calls.append(("/bbs/app/link/tree", query))
        return json.dumps(self.handler("/bbs/app/link/tree", query,
                                      self.config.protocol_mode)).encode()

    @staticmethod
    def _decode(body):
        return json.loads(body)

    @staticmethod
    def _checked_response(path, data):
        return data


class AccountSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dpapi = patch.object(accounts, "_dpapi",
                                  side_effect=lambda value, decrypt=False: value)
        self.restrict = patch.object(accounts, "_restrict_directory", return_value=None)
        self.dpapi.start()
        self.restrict.start()

    def tearDown(self):
        self.restrict.stop()
        self.dpapi.stop()
        self.temp.cleanup()

    def test_app_and_web_sessions_coexist_and_logout_is_scoped(self):
        store = accounts.AccountStore(self.root)
        store.add("same", identity="101")
        first = store.get("same")
        store.save_login("same", pkey="app-session", identity="101",
                         expected_revision=first.revision, protocol_mode="app")
        second = store.get("same")
        store.save_login("same", pkey="web-session", identity="101",
                         expected_revision=second.revision, protocol_mode="web")

        self.assertEqual(store.get_config("same", protocol="app").pkey, "app-session")
        self.assertEqual(store.get_config("same", protocol="web").pkey, "web-session")
        report = accounts._public(store.get("same"))
        self.assertTrue(report["sessions"]["app"]["present"])
        self.assertTrue(report["sessions"]["web"]["present"])

        store.logout("same", protocol="app")
        self.assertNotIn("app", store.get("same").sessions)
        self.assertIn("web", store.get("same").sessions)
        self.assertEqual(store.get_config("same", protocol="web").pkey, "web-session")

    def test_version_one_single_session_is_migrated_without_loss(self):
        legacy = {
            "alias": "legacy",
            "identity": "101",
            "pkey": "legacy-app-session",
            "config": {"protocol_mode": "app"},
            "revision": "a" * 32,
            "risk_token": "risk",
        }
        sealed = {
            "protection": "windows-dpapi",
            "value": base64.b64encode(json.dumps(legacy).encode()).decode(),
        }
        (self.root / "accounts.json").write_text(json.dumps({
            "schema_version": 1,
            "accounts": {"legacy": sealed},
        }), encoding="utf-8")

        account = accounts.AccountStore(self.root).get("legacy")
        self.assertEqual(account.sessions, {"app": "legacy-app-session"})
        self.assertEqual(account.config, {"protocol_mode": "app"})
        self.assertEqual(account.risk_token, "risk")


class ErrorContractTests(unittest.TestCase):
    def test_captcha_has_stable_code_and_stop_instruction(self):
        with self.assertRaises(XhhCaptchaError) as raised:
            Transport._check_status({"status": "show_captcha"})
        error = raised.exception
        self.assertEqual(error.code, "captcha_required")
        self.assertFalse(error.retry_allowed)
        self.assertIn("stop", error.fix.lower())

    def test_signer_error_carries_a_public_code(self):
        error = XhhSignerError("signer jar not found: missing.jar",
                               code="signer_missing",
                               fix="install or configure a signer")
        self.assertEqual(error.public()["code"], "signer_missing")
        self.assertEqual(error.public()["fix"], "install or configure a signer")


class ReaderAndBudgetTests(unittest.TestCase):
    def test_auto_reader_tries_app_then_web_and_reports_provenance(self):
        def app_handler(path, query, protocol):
            return {"result": {"link": {"id": "123", "text": ""}, "comments": []}}

        def web_handler(path, query, protocol):
            return {"result": {"link": {"id": "123", "text": "full web body",
                                        "is_full_text": 1}, "comments": []}}

        app = ClientFixture("app", app_handler)
        web = ClientFixture("web", web_handler)
        data, scope = Browser(app, clients={"app": app, "web": web}).execute(
            xhh.parser().parse_args(["post", "123", "--reader", "auto"]))

        self.assertEqual([attempt["reader"] for attempt in data["reader_attempts"]],
                         ["native", "website"])
        self.assertEqual(data["reader_attempts"][0]["body_status"], "missing")
        self.assertEqual(data["reader_attempts"][1]["body_status"], "available")
        self.assertEqual(data["source"]["reader"], "website")
        self.assertEqual(data["body"]["text"], "full web body")
        self.assertEqual(len(app.calls), 1)
        self.assertEqual(len(web.calls), 1)

    def test_explicit_native_never_falls_back(self):
        app = ClientFixture("app", lambda path, query, protocol:
                            {"result": {"link": {"id": "123", "text": ""}, "comments": []}})
        web = ClientFixture("web", lambda path, query, protocol:
                            {"result": {"link": {"id": "123", "text": "web"}, "comments": []}})
        data, scope = Browser(app, clients={"app": app, "web": web}).execute(
            xhh.parser().parse_args(["post", "123", "--reader", "native"]))
        self.assertEqual(len(web.calls), 0)
        self.assertEqual(data["body"]["status"], "missing")

    def test_limit_is_a_hard_item_budget(self):
        def handler(path, query, protocol):
            return {"result": {"links": [
                {"linkid": str(index), "create_at": index} for index in range(1, 6)
            ], "has_more": False}}

        client = ClientFixture("app", handler)
        data, scope = Browser(client).execute(
            xhh.parser().parse_args(["feed", "7214", "--limit", "2"]))
        self.assertEqual([item["id"] for item in data["items"]], ["1", "2"])
        self.assertTrue(scope["limit_reached"])
        self.assertEqual(scope["selected"], 2)
        self.assertEqual(scope["matched_total"], 5)

    def test_feed_forwards_platform_menu_values_without_closed_enums(self):
        def handler(path, query, protocol):
            self.assertEqual(query["sort_filter"], "server-value")
            self.assertEqual(query["time_range"], "7d")
            self.assertEqual(query["filter_value"], "27")
            return {"result": {"links": [], "has_more": False,
                               "sort_filter": [{"key": "server-value"}]}}

        client = ClientFixture("app", handler)
        Browser(client).execute(xhh.parser().parse_args([
            "feed", "7214", "--order", "server-value", "--time-range", "7d",
            "--platform-filter", "filter_value=27",
        ]))


if __name__ == "__main__":
    unittest.main()
