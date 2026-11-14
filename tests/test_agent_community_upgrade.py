import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import xhh
from browser import Browser
from observations import Store
from paging import ReadLimit, decode_resume
from xhh_sdk import accounts
from xhh_sdk.config import XhhConfig
from xhh_sdk.exceptions import (
    XhhAPIError,
    XhhCaptchaError,
    XhhConfigError,
    XhhRateLimitError,
    XhhSignerError,
    XhhTransportError,
)
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

    def test_explicit_missing_protocol_never_uses_the_other_session(self):
        store = accounts.AccountStore(self.root)
        store.add("sessions", identity="101")
        account = store.get("sessions")
        store.save_login(
            "sessions",
            pkey="web-session",
            identity="101",
            expected_revision=account.revision,
            protocol_mode="web",
        )

        with self.assertRaises(accounts.XhhSessionMissingError) as raised:
            store.get_config("sessions", protocol="app")
        self.assertEqual(raised.exception.code, "app_session_missing")
        self.assertFalse(raised.exception.retry_allowed)
        self.assertEqual(store.get_config("sessions").pkey, "web-session")

    def test_explicit_reader_missing_session_never_creates_a_client(self):
        store = accounts.AccountStore(self.root)
        store.add("web-only", identity="101")
        account = store.get("web-only")
        store.save_login(
            "web-only", pkey="web-session", identity="101",
            expected_revision=account.revision, protocol_mode="web")
        args = xhh.parser().parse_args([
            "--account", "web-only", "--data-dir", str(self.root),
            "post", "123", "--reader", "native",
        ])
        clients = []

        with self.assertRaises(accounts.XhhSessionMissingError) as raised:
            xhh.load_clients(
                args, store.get_config("web-only"),
                lambda config: clients.append(config))

        self.assertEqual(raised.exception.code, "app_session_missing")
        self.assertEqual(clients, [])

    def test_default_selection_falls_back_to_a_web_only_session(self):
        store = accounts.AccountStore(self.root)
        store.add("web-only", identity="101")
        account = store.get("web-only")
        store.save_login(
            "web-only",
            pkey="web-session",
            identity="101",
            expected_revision=account.revision,
            protocol_mode="web",
        )
        store.configure("web-only", {"protocol_mode": "app"})

        selected = store.get_config("web-only")
        self.assertEqual(selected.protocol_mode, "web")
        self.assertEqual(selected.pkey, "web-session")

    def test_default_selection_prefers_an_existing_protocol_mode(self):
        store = accounts.AccountStore(self.root)
        store.add("preferred", identity="101")
        first = store.get("preferred")
        store.save_login(
            "preferred", pkey="app-session", identity="101",
            expected_revision=first.revision, protocol_mode="app")
        second = store.get("preferred")
        store.save_login(
            "preferred", pkey="web-session", identity="101",
            expected_revision=second.revision, protocol_mode="web")
        store.configure("preferred", {"protocol_mode": "web"})

        selected = store.get_config("preferred")
        self.assertEqual(selected.protocol_mode, "web")
        self.assertEqual(selected.pkey, "web-session")

    def test_default_selection_fails_when_no_session_exists(self):
        store = accounts.AccountStore(self.root)
        store.add("unbound", identity="101")

        with self.assertRaises(accounts.XhhSessionMissingError) as raised:
            store.get_config("unbound")
        self.assertEqual(raised.exception.code, "session_missing")
        self.assertFalse(raised.exception.retry_allowed)

    def test_default_logout_removes_a_web_only_session(self):
        store = accounts.AccountStore(self.root)
        store.add("logout-web", identity="101")
        account = store.get("logout-web")
        store.save_login(
            "logout-web",
            pkey="web-session",
            identity="101",
            expected_revision=account.revision,
            protocol_mode="web",
        )
        store.configure("logout-web", {"protocol_mode": "app"})

        store.logout("logout-web")
        self.assertEqual(store.get("logout-web").sessions, {})


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

    def test_public_error_keeps_debug_type_without_replacing_stable_code(self):
        error = XhhConfigError("invalid configuration")
        public = error.public()
        self.assertEqual(public["code"], "configuration_error")
        self.assertEqual(public["type"], "XhhConfigError")

    def test_unexpected_errors_use_a_stable_public_code(self):
        public = xhh.public_error(ValueError("invalid fixture value"))
        self.assertEqual(public["code"], "internal_error")
        self.assertEqual(public["type"], "ValueError")
        self.assertFalse(public["retry_allowed"])

    def test_http_429_is_a_rate_limit_error(self):
        client = ClientFixture(
            "app",
            lambda path, query, protocol: self.fail("handler must not run"),
        )
        request = urllib.request.Request(
            "https://api.xiaoheihe.cn/example",
            headers={"Cookie": "must-not-appear-in-errors"},
        )
        with patch.object(
                urllib.request, "urlopen",
                side_effect=urllib.error.HTTPError(
                    request.full_url, 429, "Too Many Requests", {}, None)):
            with self.assertRaises(XhhRateLimitError) as raised:
                Transport(client.config)._open(request)
        self.assertEqual(raised.exception.code, "rate_limited")
        self.assertFalse(raised.exception.retry_allowed)


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
        self.assertIn("provenance", data)
        self.assertEqual(data["provenance"]["body"]["reader"], "website")
        self.assertEqual(data["provenance"]["metadata"]["reader"], "native")
        for attempt in data["reader_attempts"]:
            self.assertIn("route", attempt)
            self.assertIn("session_revision", attempt)
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
        self.assertEqual(scope["status"], "partial")
        self.assertIsNotNone(scope["resume"])

    def test_feed_forwards_platform_menu_values_without_closed_enums(self):
        def handler(path, query, protocol):
            self.assertEqual(query["sort_filter"], "server-value")
            self.assertEqual(query["time_range"], "7d")
            self.assertEqual(query["filter_value"], "27")
            return {"result": {"links": [], "has_more": False,
                               "sort_filter": [{"key": "server-value"}]}}

        client = ClientFixture("app", handler)
        _, scope = Browser(client).execute(xhh.parser().parse_args([
            "feed", "7214", "--order", "server-value", "--time-range", "7d",
            "--platform-filter", "filter_value=27",
        ]))
        self.assertEqual(scope["platform_order"], "server-value")
        self.assertEqual(scope["platform_sort_filter"], "server-value")

    def test_explicit_html_is_a_credential_free_public_get(self):
        client = ClientFixture(
            "web",
            lambda path, query, protocol: self.fail("signed reader was called"),
        )
        client.config.pkey = "must-not-leak"
        seen = {}
        request_type = urllib.request.Request

        def capture_request(url, *, headers, method):
            request = request_type(url, headers=headers, method=method)
            seen["request"] = request
            return request

        with patch("browser.urllib.request.Request", side_effect=capture_request), \
                patch("browser.read_http", return_value=b"<article><p>public body</p></article>"):
            data, scope = Browser(client).execute(
                xhh.parser().parse_args(["post", "123", "--reader", "html"]))

        self.assertEqual(seen["request"].get_header("Cookie"), None)
        self.assertEqual(seen["request"].get_method(), "GET")
        self.assertFalse(any(header.casefold() == "cookie"
                             for header in seen["request"].headers))
        self.assertEqual(data["source"]["reader"], "html")
        self.assertEqual(data["source"]["protocol"], "public")
        self.assertFalse(data["source"]["credentials_sent"])
        self.assertEqual(data["body"]["text"], "public body")
        self.assertEqual(scope["status"], "complete")
        self.assertEqual(client.calls, [])

    def test_auto_works_with_a_web_only_client_set(self):
        web = ClientFixture(
            "web",
            lambda path, query, protocol: {
                "result": {
                    "link": {"id": "123", "text": "web body", "is_full_text": 1},
                    "comments": [],
                },
            },
        )
        data, scope = Browser(web, clients={"web": web}).execute(
            xhh.parser().parse_args(["post", "123", "--reader", "auto"]))

        self.assertEqual([attempt["reader"] for attempt in data["reader_attempts"]],
                         ["website"])
        self.assertEqual(data["source"]["reader"], "website")
        self.assertEqual(scope["status"], "complete")

    def test_auto_stops_after_positive_complete_body_evidence(self):
        app = ClientFixture(
            "app",
            lambda path, query, protocol: {
                "result": {
                    "link": {"id": "123", "text": "complete app body",
                             "is_full_text": 1},
                    "comments": [],
                },
            },
        )
        web = ClientFixture(
            "web",
            lambda path, query, protocol: self.fail(
                "Web reader must not run after positive App completeness"),
        )
        data, scope = Browser(app, clients={"app": app, "web": web}).execute(
            xhh.parser().parse_args(["post", "123", "--reader", "auto"]))

        self.assertEqual(data["body"]["completeness"], "complete")
        self.assertEqual([attempt["reader"] for attempt in data["reader_attempts"]],
                         ["native"])
        self.assertEqual(web.calls, [])
        self.assertEqual(scope["status"], "complete")

    def test_auto_retryable_app_failure_falls_back_to_web(self):
        def app_handler(path, query, protocol):
            raise XhhTransportError("temporary App transport failure")

        web = ClientFixture(
            "web",
            lambda path, query, protocol: {
                "result": {
                    "link": {"id": "123", "text": "web body",
                             "is_full_text": 1},
                    "comments": [],
                },
            },
        )
        app = ClientFixture("app", app_handler)
        data, scope = Browser(app, clients={"app": app, "web": web}).execute(
            xhh.parser().parse_args(["post", "123", "--reader", "auto"]))

        self.assertEqual(
            [attempt["status"] for attempt in data["reader_attempts"]],
            ["error", "ok"])
        self.assertEqual(data["source"]["reader"], "website")
        self.assertEqual(data["body"]["text"], "web body")
        self.assertEqual(scope["status"], "complete")

    def test_reader_fallback_uses_request_budget_without_consuming_pages(self):
        app = ClientFixture(
            "app",
            lambda path, query, protocol: {
                "result": {"link": {"id": "123", "text": ""}, "comments": []},
            },
        )
        web = ClientFixture(
            "web",
            lambda path, query, protocol: self.fail(
                "request budget must stop before the Web reader"),
        )
        data, scope = Browser(app, clients={"app": app, "web": web}).execute(
            xhh.parser().parse_args([
                "post", "123", "--reader", "auto", "--requests", "1",
            ]))

        self.assertEqual(len(app.calls), 1)
        self.assertEqual(web.calls, [])
        self.assertEqual(scope["status"], "partial")
        self.assertEqual(scope["budget"]["used_requests"], 1)
        self.assertEqual(scope["budget"]["pages_used"], 0)
        self.assertEqual(data["reader_attempts"][-1]["status"], "budget_stop")

    def test_identity_and_reader_attempts_share_the_request_budget(self):
        class AccountClient(ClientFixture):
            def account_info(self):
                self.calls.append(("/account/info", {}))
                return {"userid": "101"}

        app = AccountClient(
            "app",
            lambda path, query, protocol: {
                "result": {"link": {"id": "123", "text": ""},
                           "comments": []},
            },
        )
        web = ClientFixture(
            "web",
            lambda path, query, protocol: {
                "result": {"link": {"id": "123", "text": ""},
                           "comments": []},
            },
        )
        _, scope = Browser(app, clients={"app": app, "web": web}).execute(
            xhh.parser().parse_args([
                "post", "123", "--reader", "auto", "--requests", "3",
            ]),
            verify_account=True,
        )

        self.assertEqual(len(app.calls) + len(web.calls), 3)
        self.assertEqual(scope["budget"]["used_requests"], 3)
        self.assertEqual(scope["budget"]["pages_used"], 0)
        self.assertEqual(scope["status"], "partial")

    def test_default_page_budget_does_not_block_reader_fallback(self):
        app = ClientFixture(
            "app",
            lambda path, query, protocol: {
                "result": {"link": {"id": "123", "text": ""}, "comments": []},
            },
        )
        web = ClientFixture(
            "web",
            lambda path, query, protocol: {
                "result": {
                    "link": {"id": "123", "text": "web body", "is_full_text": 1},
                    "comments": [],
                },
            },
        )
        data, scope = Browser(app, clients={"app": app, "web": web}).execute(
            xhh.parser().parse_args(["post", "123", "--reader", "auto"]))

        self.assertEqual(len(app.calls), 1)
        self.assertEqual(len(web.calls), 1)
        self.assertEqual(scope["budget"]["pages_used"], 0)

    def test_provider_protocol_mismatch_fails_before_any_request(self):
        app = ClientFixture("app", lambda *args: self.fail("request was sent"))
        web = ClientFixture("web", lambda *args: self.fail("request was sent"))
        with self.assertRaises(XhhConfigError):
            Browser(app, clients={"app": app, "web": web}).execute(
                xhh.parser().parse_args([
                    "--protocol", "app", "post", "123", "--reader", "website",
                ]))
        self.assertEqual(app.calls, [])
        self.assertEqual(web.calls, [])

    def test_provider_mismatch_precedes_snapshot_identity_verification(self):
        class AccountClient(ClientFixture):
            def account_info(self):
                self.calls.append(("/account/info", {}))
                return {"userid": "101"}

        app = AccountClient("app", lambda *args: self.fail("request was sent"))
        web = ClientFixture("web", lambda *args: self.fail("request was sent"))
        with self.assertRaises(XhhConfigError):
            Browser(app, clients={"app": app, "web": web}).execute(
                xhh.parser().parse_args([
                    "--protocol", "app", "post", "123",
                    "--reader", "website",
                ]),
                verify_account=True,
            )
        self.assertEqual(app.calls, [])
        self.assertEqual(web.calls, [])

    def test_mid_page_limit_resumes_at_the_same_upstream_position(self):
        def handler(path, query, protocol):
            return {
                "result": {
                    "comments": [
                        {"comment": [
                            {"commentid": str(index), "text": str(index)}
                        ]}
                        for index in range(1, 6)
                    ],
                    "total_page": 1,
                },
            }

        client = ClientFixture("web", handler)
        first, first_scope = Browser(client).execute(
            xhh.parser().parse_args([
                "comments", "123", "--limit", "2",
            ]))
        self.assertEqual([item["id"] for item in first["items"]], ["1", "2"])
        self.assertEqual(first_scope["status"], "partial")
        self.assertIsNotNone(first_scope["resume"])
        token = decode_resume(first_scope["resume"], "comments", "101")
        self.assertEqual(token["position"], 1)
        self.assertEqual(token["item_window"],
                         {"position": 1,
                          "consumed": ["comment:1", "comment:2"]})

        second, second_scope = Browser(client).execute(
            xhh.parser().parse_args([
                "comments", "123", "--limit", "2",
                "--resume", first_scope["resume"],
            ]))
        self.assertEqual([item["id"] for item in second["items"]], ["3", "4"])
        self.assertEqual([call[1]["page"] for call in client.calls], ["1", "1"])
        self.assertEqual(second_scope["status"], "partial")

    def test_replies_limit_resumes_with_a_null_initial_cursor(self):
        def handler(path, query, protocol):
            return {
                "result": {
                    "comments": [
                        {"commentid": str(index), "text": str(index)}
                        for index in range(1, 6)
                    ],
                    "has_more": True,
                    "lastval": "next-page",
                },
            }

        client = ClientFixture("web", handler)
        first, first_scope = Browser(client).execute(
            xhh.parser().parse_args([
                "replies", "123", "--post-id", "456", "--limit", "2",
            ]))
        self.assertEqual([item["id"] for item in first["items"]],
                         ["1", "2"])
        self.assertEqual(first_scope["status"], "partial")
        token = decode_resume(first_scope["resume"], "replies", "101")
        self.assertIsNone(token["position"])
        self.assertEqual(token["item_window"], {
            "position": None,
            "consumed": ["comment:1", "comment:2"],
        })

        second, second_scope = Browser(client).execute(
            xhh.parser().parse_args([
                "replies", "123", "--post-id", "456", "--limit", "2",
                "--resume", first_scope["resume"],
            ]))
        self.assertEqual([item["id"] for item in second["items"]],
                         ["3", "4"])
        self.assertEqual(second_scope["status"], "partial")
        self.assertEqual(
            [call[1].get("lastval") for call in client.calls],
            [None, None],
        )

    def test_item_budget_has_a_stable_public_code(self):
        error = ReadLimit("item_budget")
        self.assertEqual(error.code, "item_budget_exhausted")
        self.assertEqual(error.public()["code"], "item_budget_exhausted")

    def test_thread_limit_resumes_at_the_same_comment_page(self):
        case = self

        class ThreadFixture(ClientFixture):
            def __init__(self):
                def handler(path, query, protocol):
                    case.assertEqual(path, "/bbs/app/link/tree")
                    records = [
                        {"commentid": "9", "text": "root", "child_num": 4,
                         "has_more": 0},
                        *[{"commentid": str(index), "text": "reply",
                           "replyid": "9"} for index in range(10, 14)],
                    ]
                    return {
                        "result": {
                            "comments": [{"comment": records}],
                            "total_page": 1,
                        },
                    }

                super().__init__("web", handler)

        client = ThreadFixture()
        first, first_scope = Browser(client).execute(
            xhh.parser().parse_args([
                "thread", "123", "9", "--around", "11", "--limit", "2",
            ]))
        self.assertEqual([item["id"] for item in first["items"]],
                         ["9", "10"])
        self.assertEqual(first_scope["status"], "partial")
        self.assertEqual(first_scope["budget"]["items_used"], 2)
        self.assertIsNotNone(first_scope["resume"])

        second, second_scope = Browser(client).execute(
            xhh.parser().parse_args([
                "thread", "123", "9", "--around", "11", "--limit", "2",
                "--resume", first_scope["resume"],
            ]))
        self.assertEqual([item["id"] for item in second["items"]],
                         ["9", "11"])
        self.assertEqual([call[1]["page"] for call in client.calls],
                         ["1", "1"])
        self.assertEqual(second_scope["status"], "partial")
        self.assertEqual(second_scope["budget"]["items_used"], 2)

    def test_thread_limit_one_cannot_resume_without_dropping_remainder(self):
        class ThreadFixture(ClientFixture):
            def __init__(self):
                def handler(path, query, protocol):
                    records = [
                        {"commentid": "9", "text": "root", "child_num": 1,
                         "has_more": 0},
                        {"commentid": "10", "text": "reply", "replyid": "9"},
                    ]
                    return {
                        "result": {
                            "comments": [{"comment": records}],
                            "total_page": 1,
                        },
                    }

                super().__init__("web", handler)

        data, scope = Browser(ThreadFixture()).execute(
            xhh.parser().parse_args([
                "thread", "123", "9", "--around", "10", "--limit", "1",
            ]))

        self.assertEqual([item["id"] for item in data["items"]], ["9"])
        self.assertEqual(scope["status"], "failed")
        self.assertEqual(scope["error"]["code"], "unsafe_item_continuation")
        self.assertIsNone(scope["resume"])


class StopAndSnapshotProjectionTests(unittest.TestCase):
    class StopFixture(ClientFixture):
        def __init__(self, page_two_error):
            def handler(path, query, protocol):
                if int(query.get("page", "1")) == 2:
                    raise page_two_error()
                return {
                    "result": {
                        "comments": [
                            {"comment": [{"commentid": "1", "text": "first"}]}
                        ],
                        "total_page": 3,
                    },
                }

            super().__init__("web", handler)

        def account_info(self):
            self.calls.append(("/account/info", {}))
            return {"userid": self.config.heybox_id}

    def test_later_captcha_stops_without_resume_or_further_requests(self):
        client = self.StopFixture(
            lambda: XhhCaptchaError("platform requires captcha"))
        data, scope = Browser(client).execute(
            xhh.parser().parse_args([
                "comments", "123", "--pages", "3",
            ]))

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(scope["status"], "blocked")
        self.assertEqual(scope["error"]["code"], "captcha_required")
        self.assertIsNone(scope["resume"])

    def test_later_rate_limit_stops_without_resume_or_further_requests(self):
        client = self.StopFixture(
            lambda: XhhRateLimitError("platform throttled the request"))
        data, scope = Browser(client).execute(
            xhh.parser().parse_args([
                "comments", "123", "--pages", "3",
            ]))

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(scope["status"], "blocked")
        self.assertEqual(scope["error"]["code"], "rate_limited")
        self.assertIsNone(scope["resume"])

    def test_http_rate_limit_status_stops_without_resume(self):
        client = self.StopFixture(
            lambda: XhhAPIError("HTTP 429", status="HTTP_429"))
        data, scope = Browser(client).execute(
            xhh.parser().parse_args([
                "comments", "123", "--pages", "3",
            ]))

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(scope["status"], "blocked")
        self.assertEqual(scope["error"]["code"], "rate_limited")
        self.assertIsNone(scope["resume"])

    def test_snapshot_projects_blocked_stops_without_resume(self):
        cases = {
            "captcha": (
                lambda: XhhCaptchaError("platform requires captcha"),
                "captcha_required",
            ),
            "rate": (
                lambda: XhhRateLimitError("platform throttled the request"),
                "rate_limited",
            ),
        }
        for name, (factory, code_value) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                client = self.StopFixture(factory)
                code, value = self.run_cli(
                    root,
                    ["comments", "123", "--pages", "3",
                     "--snapshot", "blocked"],
                    factory=lambda config: client,
                )

                self.assertEqual(code, 1)
                self.assertFalse(value["ok"])
                self.assertEqual(value["coverage"]["status"], "blocked")
                self.assertEqual(value["error"]["code"], code_value)
                self.assertIsNone(value["coverage"]["resume"])
                saved = Store(root).get("101", "blocked")
                self.assertEqual(saved["coverage"]["status"], "blocked")
                self.assertIsNone(saved["coverage"]["resume"])
                self.assertIsNone(saved["pending_response"])

    def run_cli(self, root, arguments, *, factory):
        config = root / "fixture.json"
        config.write_text(json.dumps({
            "pkey": "fixture",
            "heybox_id": "101",
            "protocol_mode": "web",
        }), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = xhh.main(
                ["--config", str(config), "--data-dir", str(root), *arguments],
                client_factory=factory,
            )
        return code, json.loads(output.getvalue())

    def test_legacy_budget_pending_replays_as_successful_partial(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root)
            coverage = {
                "completion": "more",
                "stop_reason": "page_budget",
                "resume": "legacy-budget-token",
            }
            store.save(
                "101", "legacy-budget", "query",
                {"items": [{"id": "1"}]},
                coverage,
                ["comments", "123"],
                pending_response={
                    "ok": False,
                    "data": {"items": [{"id": "1"}]},
                    "coverage": coverage,
                    "error": {
                        "code": "page_budget_exhausted",
                        "message": "page budget stopped the read",
                        "retry_allowed": True,
                    },
                },
            )
            code, value = self.run_cli(
                root,
                ["snapshot", "resume", "legacy-budget"],
                factory=lambda config: self.fail("pending replay must be offline"),
            )

        self.assertEqual(code, 0)
        self.assertTrue(value["ok"])
        self.assertEqual(value["coverage"]["status"], "partial")
        self.assertEqual(value["coverage"]["resume"], "legacy-budget-token")
        self.assertIsNone(value["error"])

    def test_legacy_captcha_pending_replays_without_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root)
            coverage = {
                "completion": "more",
                "stop_reason": "platform_error",
                "resume": "unsafe-legacy-token",
                "error": {
                    "code": "platform_error",
                    "message": "captcha required",
                    "status": "show_captcha",
                    "retry_allowed": True,
                },
            }
            store.save(
                "101", "legacy-captcha", "query",
                {"items": [{"id": "1"}]},
                coverage,
                ["comments", "123"],
                pending_response={
                    "ok": False,
                    "data": {"items": [{"id": "1"}]},
                    "coverage": coverage,
                    "error": coverage["error"],
                },
            )
            code, value = self.run_cli(
                root,
                ["snapshot", "resume", "legacy-captcha"],
                factory=lambda config: self.fail("pending replay must be offline"),
            )

        self.assertEqual(code, 1)
        self.assertFalse(value["ok"])
        self.assertEqual(value["coverage"]["status"], "blocked")
        self.assertIsNone(value["coverage"]["resume"])
        self.assertEqual(value["error"]["code"], "captcha_required")

    def test_live_budget_stops_exit_zero_with_partial_coverage(self):
        class BudgetFixture(ClientFixture):
            def __init__(self, mode):
                self.mode = mode

                def handler(path, query, protocol):
                    page = int(query.get("page", "1"))
                    if self.mode == "time" and page == 2:
                        raise ReadLimit("time_budget")
                    count = 3 if self.mode == "item" else 1
                    return {
                        "result": {
                            "comments": [
                                {"comment": [{
                                    "commentid": str(page * 10 + index),
                                    "text": "comment",
                                }]}
                                for index in range(count)
                            ],
                            "total_page": 1 if self.mode == "item" else 2,
                        },
                    }

                super().__init__("web", handler)

        cases = {
            "page": ["comments", "123"],
            "request": [
                "comments", "123", "--pages", "2", "--requests", "1",
            ],
            "time": ["comments", "123", "--pages", "2"],
            "item": ["comments", "123", "--limit", "1"],
        }
        for mode, arguments in cases.items():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                code, value = self.run_cli(
                    Path(temporary),
                    arguments,
                    factory=lambda config, mode=mode: BudgetFixture(mode),
                )
                self.assertEqual(code, 0)
                self.assertTrue(value["ok"])
                self.assertEqual(value["coverage"]["status"], "partial")
                self.assertIsNotNone(value["coverage"]["resume"])
                self.assertIsNone(value["error"])


if __name__ == "__main__":
    unittest.main()
