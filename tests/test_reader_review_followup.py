import base64
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "scripts"),
                str(Path(__file__).resolve().parent)]
import browser
import network_io
import xhh
from observations import Store
from paging import Budget, ReadLimit, decode_resume, project_coverage
from xhh_sdk import accounts, cli
from xhh_sdk.client import XhhClient
from xhh_sdk.config import XhhConfig
from xhh_sdk.exceptions import (
    XhhAPIError,
    XhhCaptchaError,
    XhhConfigError,
    XhhTransportError,
)
from xhh_sdk.transport import Transport


class ClientFixture:
    def __init__(self, protocol, handler):
        self.config = XhhConfig(
            pkey=protocol + "-pkey",
            heybox_id="101",
            protocol_mode=protocol,
        )
        self.transport = self
        self.handler = handler
        self.calls = []

    def signed_request(self, path, query=None):
        self.calls.append((path, query))
        return self.handler(path, query, self.config.protocol_mode)

    def account_info(self):
        self.calls.append(("/account/info", None))
        return {"userid": self.config.heybox_id}

    def _open(self, request):
        query = {
            key: values[-1]
            for key, values in parse_qs(urlsplit(request.full_url).query).items()
        }
        path = "/bbs/app/link/tree"
        self.calls.append((path, query))
        return json.dumps(
            self.handler(path, query, self.config.protocol_mode)
        ).encode()

    @staticmethod
    def _decode(body):
        return json.loads(body)

    @staticmethod
    def _checked_response(path, data):
        return data


def subprocess_result(value):
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(value).encode("utf-8"),
        stderr=b"",
    )


class WireBoundaryTests(unittest.TestCase):
    def test_app_account_identity_uses_the_redirect_free_route(self):
        class RecordingTransport:
            def __init__(self):
                self.paths = []

            def signed_request(self, path, query=None):
                self.paths.append(path)
                return {"result": {"userid": "101"}}

        transport = RecordingTransport()
        client = XhhClient(
            XhhConfig(
                pkey="fixture",
                heybox_id="101",
                protocol_mode="app",
            ),
            transport=transport,
        )

        self.assertEqual(client.account_info(), {"userid": "101"})
        self.assertEqual(transport.paths, ["/account/info/"])

    def test_redirect_handler_refuses_a_second_wire_request_at_limit_one(self):
        start = "https://www.xiaoheihe.cn/app/bbs/link/123"
        request = Request(start)
        handler = network_io.PublicXiaoheiheRedirects(start, wire_limit=1)

        with self.assertRaises(network_io.NetworkRequestLimit):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://www.xiaoheihe.cn/app/bbs/link/124",
            )

    def test_reported_wire_requests_consume_the_remaining_budget(self):
        config = XhhConfig(
            pkey="fixture", heybox_id="101", protocol_mode="web")
        transport = Transport(config)
        client = SimpleNamespace(config=config, transport=transport)
        budget = Budget(requests=2)

        def relay(request, timeout, *, wire_limit=None, wire_counter=None,
                  **kwargs):
            self.assertEqual(wire_limit, 2)
            self.assertIsNotNone(wire_counter)
            wire_counter(2)
            return b"body"

        request = Request("https://api.xiaoheihe.cn/bbs/app/link/tree")
        with patch("paging.read_http", side_effect=relay):
            budget.call(client, lambda: transport._open(request))
            self.assertEqual(budget.used, 2)
            with self.assertRaises(ReadLimit) as raised:
                budget.call(client, lambda: transport._open(request))
        self.assertEqual(raised.exception.reason, "request_budget")

    def test_separate_bounded_opens_accumulate_against_one_ceiling(self):
        config = XhhConfig(
            pkey="fixture", heybox_id="101", protocol_mode="web")
        transport = Transport(config)
        client = SimpleNamespace(config=config, transport=transport)
        budget = Budget(requests=1)
        sent = []

        def relay(request, timeout, *, wire_limit=None, wire_counter=None,
                  **kwargs):
            self.assertEqual(wire_limit, 1)
            sent.append(request.full_url)
            wire_counter(1)
            return b"body"

        request = Request("https://api.xiaoheihe.cn/bbs/app/link/tree")

        def two_opens():
            transport._open(request)
            transport._open(request)

        with patch("paging.read_http", side_effect=relay):
            with self.assertRaises(ReadLimit) as raised:
                budget.call(client, two_opens)

        self.assertEqual(raised.exception.reason, "request_budget")
        self.assertEqual(len(sent), 1)
        self.assertEqual(budget.used, 1)

    def test_public_html_wire_redirects_consume_the_request_budget(self):
        client = ClientFixture(
            "web",
            lambda path, query, protocol: self.fail("signed request"),
        )
        budget = Budget(requests=1)

        def public_get(request, timeout, **kwargs):
            self.assertEqual(kwargs["profile"], "public")
            self.assertEqual(kwargs["wire_limit"], 1)
            kwargs["wire_counter"](2)
            return b"<article>body</article>"

        with patch("browser.read_http", side_effect=public_get):
            with self.assertRaises(ReadLimit) as raised:
                browser.Browser(client).public_html("123", budget)

        self.assertEqual(raised.exception.reason, "request_budget")
        self.assertEqual(budget.used, 1)

    def test_public_http_403_is_an_access_failure_not_session_expiry(self):
        response = subprocess_result({
            "ok": False,
            "kind": "HTTPError",
            "status": "HTTP_403",
            "wire_requests": 1,
        })
        request = Request("https://www.xiaoheihe.cn/app/bbs/link/123")
        with patch("network_io.subprocess.run", return_value=response):
            with self.assertRaises(XhhAPIError) as raised:
                network_io.read_http(request, 1, profile="public")

        error = raised.exception
        self.assertEqual(error.code, "http_access_denied")
        self.assertEqual(error.status, "HTTP_403")
        self.assertFalse(error.retry_allowed)
        self.assertEqual(xhh.public_error(error, "web")["code"],
                         "http_access_denied")

    def test_public_body_over_four_mib_is_rejected(self):
        body = b"x" * (4 * 1024 * 1024 + 1)
        response = subprocess_result({
            "ok": True,
            "body": base64.b64encode(body).decode("ascii"),
            "wire_requests": 1,
        })
        request = Request("https://www.xiaoheihe.cn/app/bbs/link/123")
        with patch("network_io.subprocess.run", return_value=response):
            with self.assertRaises(XhhAPIError) as raised:
                network_io.read_http(request, 1, profile="public")
        self.assertEqual(raised.exception.code, "public_response_too_large")
        self.assertFalse(raised.exception.retry_allowed)


class ReaderEvidenceTests(unittest.TestCase):
    def test_paged_delivery_keeps_invalid_record_evidence_from_every_page(self):
        class PagedBrowser(browser.Browser):
            def page(self, args, budget):
                if args.offset == 0:
                    return (
                        {"items": [{"id": "1"}, {"id": "2"}]},
                        {
                            "completion": "more",
                            "gaps": ["invalid_post_records"],
                            "next": 1,
                            "returned": 2,
                            "invalid": 1,
                            "excluded": 1,
                        },
                    )
                return (
                    {"items": [{"id": "3"}]},
                    {
                        "completion": "complete",
                        "gaps": [],
                        "next": None,
                        "returned": 1,
                        "invalid": 0,
                        "excluded": 0,
                    },
                )

        client = ClientFixture("app", lambda *args: self.fail("unused"))
        data, scope = PagedBrowser(client).execute(
            xhh.parser().parse_args([
                "stories", "--pages", "2", "--limit", "10",
            ]))

        self.assertEqual([item["id"] for item in data["items"]], ["1", "2", "3"])
        self.assertEqual(scope["gaps"], ["invalid_post_records"])
        self.assertEqual(scope["invalid"], 1)
        self.assertEqual(scope["excluded"], 1)

    def test_auto_reader_stops_on_platform_block_even_when_retry_flag_is_true(self):
        for status, expected in (
            ("show_captcha", "captcha_required"),
            ("HTTP_429", "rate_limited"),
        ):
            with self.subTest(status=status):
                app = ClientFixture(
                    "app",
                    lambda *args: self.fail("signed App request"),
                )
                web = ClientFixture(
                    "web",
                    lambda *args: self.fail("signed Web request"),
                )
                calls = []
                evidence = {
                    "route": "/bbs/app/link/tree",
                    "protocol": "app",
                    "observed_at": 0,
                    "configured_uid": "101",
                    "identity_verified": False,
                    "reader": "native",
                }

                def incomplete(args, budget):
                    calls.append("native")
                    return browser.document(
                        {"id": "123", "text": ""}, evidence), {}

                def blocked(args, budget):
                    calls.append("website")
                    raise XhhAPIError(
                        f"platform stopped with {status}",
                        status=status,
                        retry_allowed=True,
                    )

                plan = [
                    browser.ReaderSpec(
                        "native", "app", "/bbs/app/link/tree", incomplete),
                    browser.ReaderSpec(
                        "website", "web", "/bbs/app/link/tree", blocked),
                ]
                data, scope = browser.Browser(
                    app, clients={"app": app, "web": web}).post(
                        xhh.parser().parse_args([
                            "post", "123", "--reader", "auto",
                        ]),
                        Budget(requests=2),
                        plan=plan,
                    )

                self.assertEqual(data["body"]["status"], "missing")
                self.assertEqual(scope["status"], "blocked")
                self.assertEqual(scope["error"]["code"], expected)
                self.assertIsNone(scope["resume"])
                self.assertEqual(calls, ["native", "website"])

    def test_later_terminal_reader_error_outranks_earlier_transport_failure(self):
        app = ClientFixture("app", lambda *args: self.fail("signed request"))
        web = ClientFixture("web", lambda *args: self.fail("signed request"))
        calls = []

        def transport_failure(args, budget):
            calls.append("native")
            raise XhhTransportError("temporary network failure")

        def captcha_failure(args, budget):
            calls.append("website")
            raise XhhCaptchaError("platform requires captcha")

        def forbidden_third_reader(args, budget):
            calls.append("html")
            raise AssertionError("reader after terminal error was attempted")

        plan = [
            browser.ReaderSpec(
                "native", "app", "/bbs/app/link/tree", transport_failure),
            browser.ReaderSpec(
                "website", "web", "/bbs/app/link/tree", captcha_failure),
            browser.ReaderSpec(
                "html", "public", "/app/bbs/link/{post_id}",
                forbidden_third_reader),
        ]
        value = browser.Browser(
            app, clients={"app": app, "web": web})

        with self.assertRaises(XhhCaptchaError):
            value.post(
                xhh.parser().parse_args(["post", "123", "--reader", "auto"]),
                Budget(requests=3),
                plan=plan,
            )

        self.assertEqual(calls, ["native", "website"])

    def test_identity_evidence_stays_with_the_client_that_verified_it(self):
        app = ClientFixture(
            "app",
            lambda path, query, protocol: {
                "result": {"link": {"id": "123", "title": "from app",
                                    "text": ""}},
            },
        )
        web = ClientFixture(
            "web",
            lambda path, query, protocol: {
                "result": {"link": {"id": "123", "title": "from web",
                                    "text": "web body", "is_full_text": 1}},
            },
        )

        data, scope = browser.Browser(
            app, clients={"app": app, "web": web}).execute(
                xhh.parser().parse_args([
                    "post", "123", "--reader", "auto",
                ]),
                verify_account=True,
            )

        self.assertEqual(scope["status"], "complete")
        self.assertTrue(data["reader_attempts"][0]["identity_verified"])
        self.assertFalse(data["reader_attempts"][1]["identity_verified"])
        self.assertTrue(data["provenance"]["metadata"]["identity_verified"])
        self.assertFalse(data["provenance"]["body"]["identity_verified"])
        self.assertFalse(data["source"]["identity_verified"])

    def test_explicit_html_without_a_body_is_a_stable_failure(self):
        public = b"<html><head><title>Xiaoheihe</title></head><body><nav>menu</nav></body></html>"
        output = io.StringIO()
        with patch("browser.read_http", return_value=public), \
                contextlib.redirect_stdout(output):
            code = xhh.main([
                "post", "123", "--reader", "html", "--requests", "1",
                "--section", "body",
            ])
        value = json.loads(output.getvalue())

        self.assertEqual(code, 1, value)
        self.assertFalse(value["ok"])
        self.assertEqual(value["coverage"]["status"], "failed")
        self.assertEqual(value["error"]["code"], "body_missing")
        self.assertEqual(value["data"]["body"]["status"], "missing")
        self.assertEqual(
            value["data"]["reader_attempts"][-1]["error"]["code"],
            "body_missing",
        )

    def test_auto_resume_starts_at_the_next_reader(self):
        app = ClientFixture(
            "app",
            lambda path, query, protocol: {
                "result": {"link": {"id": "123", "text": ""}},
            },
        )
        web = ClientFixture(
            "web",
            lambda path, query, protocol: {
                "result": {"link": {"id": "123", "text": "web body",
                                    "is_full_text": 1}},
            },
        )
        first, scope = browser.Browser(
            app, clients={"app": app, "web": web}).execute(
                xhh.parser().parse_args([
                    "post", "123", "--reader", "auto", "--requests", "1",
                ]),
            )
        token = decode_resume(scope["resume"], "post", "101")
        self.assertEqual(token["reader_offset"], 1)

        app.calls.clear()
        web.calls.clear()
        second, second_scope = browser.Browser(
            app, clients={"app": app, "web": web}).execute(
                xhh.parser().parse_args([
                    "post", "123", "--reader", "auto", "--requests", "1",
                    "--resume", scope["resume"],
                ]),
            )

        self.assertEqual(app.calls, [])
        self.assertEqual(len(web.calls), 1)
        self.assertEqual(second["body"]["text"], "web body")
        self.assertEqual(second_scope["status"], "complete")

    def test_auto_resume_rejects_a_changed_reader_plan(self):
        app = ClientFixture(
            "app",
            lambda path, query, protocol: {
                "result": {"link": {"id": "123", "text": ""}},
            },
        )
        web = ClientFixture(
            "web",
            lambda path, query, protocol: {
                "result": {"link": {"id": "123", "text": "web body",
                                    "is_full_text": 1}},
            },
        )
        _, scope = browser.Browser(
            web, clients={"app": app, "web": web}).execute(
                xhh.parser().parse_args([
                    "post", "123", "--reader", "auto", "--requests", "1",
                ]),
            )

        web.calls.clear()
        with patch(
                "browser.read_http",
                return_value=b"<article><p>public body</p></article>"):
            with self.assertRaises(XhhConfigError):
                browser.Browser(web).execute(
                    xhh.parser().parse_args([
                        "post", "123", "--reader", "auto", "--requests", "1",
                        "--resume", scope["resume"],
                    ]),
                )

        self.assertEqual(web.calls, [])

    def test_steam_score_and_comment_score_remain_distinct(self):
        value = browser.game({
            "steam_appid": "123",
            "name": "Game",
            "score": 9.2,
            "score_comment": 318,
        })
        self.assertEqual(value["score"], 9.2)
        self.assertEqual(value["score_comment"], 318)


class PublicDocumentTests(unittest.TestCase):
    def run_html(self, page):
        client = ClientFixture(
            "web",
            lambda path, query, protocol: self.fail("signed request"),
        )
        with patch("browser.read_http", return_value=page):
            return browser.Browser(client).execute(
                xhh.parser().parse_args([
                    "post", "123", "--reader", "html", "--requests", "1",
                ]),
            )

    def test_public_html_does_not_invent_title_or_author(self):
        data, scope = self.run_html(
            b"<html><head><title>Xiaoheihe</title></head>"
            b"<body><article><p>body</p></article></body></html>"
        )
        self.assertIsNone(data["title"])
        self.assertIsNone(data["author"]["uid"])
        self.assertIsNone(data["author"]["name"])
        self.assertIn("html_title_not_found", data["metadata_gaps"])
        self.assertIn("html_author_not_found", data["metadata_gaps"])
        self.assertIn("html_title_not_found", scope["gaps"])
        self.assertIn("html_author_not_found", scope["gaps"])

    def test_public_html_uses_structured_title_and_author_evidence(self):
        metadata = json.dumps({
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": "Real post title",
            "author": {
                "@type": "Person",
                "name": "Author name",
                "identifier": "456",
            },
        })
        page = (
            '<html><head><title>Xiaoheihe</title>'
            '<script type="application/ld+json">' + metadata + '</script>'
            '</head><body><article><p>body</p></article></body></html>'
        ).encode("utf-8")
        data, _ = self.run_html(page)
        self.assertEqual(data["title"], "Real post title")
        self.assertEqual(data["author"]["uid"], "456")
        self.assertEqual(data["author"]["name"], "Author name")
        self.assertNotIn("html_title_not_found", data["metadata_gaps"])
        self.assertNotIn("html_author_not_found", data["metadata_gaps"])

    def test_public_html_stops_at_article_boundary_across_void_elements(self):
        data, _ = self.run_html(
            b"<html><body><article><p>article body</p>"
            b"<img src=\"https://example.com/image.png\"><br>"
            b"</article><footer>footer text</footer></body></html>"
        )

        self.assertEqual(data["body"]["text"], "article body")
        self.assertNotIn("footer text", data["body"]["text"])


class SnapshotAndStatusTests(unittest.TestCase):
    def test_pending_block_error_outside_coverage_controls_replayed_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root)
            error = {
                "status": "show_captcha",
                "message": "platform requires captcha",
                "retry_allowed": True,
            }
            store.save(
                "101", "blocked", "query",
                {"items": [{"id": "1"}]},
                {"status": "partial", "resume": "unsafe-token"},
                ["comments", "123"],
                pending_response={
                    "ok": True,
                    "data": {"items": [{"id": "1"}]},
                    "coverage": {"status": "partial", "resume": "unsafe-token"},
                    "error": error,
                },
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = xhh.main([
                    "--data-dir", str(root),
                    "snapshot", "resume", "blocked",
                ], client_factory=lambda config: self.fail(
                    "offline replay requested a client"))
            value = json.loads(output.getvalue())
            saved = Store(root).get("101", "blocked")

        self.assertEqual(code, 1, value)
        self.assertFalse(value["ok"])
        self.assertEqual(value["coverage"]["status"], "blocked")
        self.assertEqual(value["error"]["code"], "captcha_required")
        self.assertIsNone(value["coverage"]["resume"])
        self.assertIsNone(saved["pending_response"])

    def test_pending_snapshot_replays_before_config_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root)
            pending = {
                "ok": True,
                "data": {
                    "id": "123",
                    "body": {"text": "saved body", "status": "available"},
                },
                "coverage": {"status": "complete", "resume": None},
                "error": None,
            }
            store.save(
                "0", "public", "query", pending["data"],
                pending["coverage"], ["post", "123", "--reader", "html"],
                pending_response=pending,
            )
            output = io.StringIO()
            with patch("xhh.load_config",
                       side_effect=XhhConfigError("must not load")), \
                    contextlib.redirect_stdout(output):
                code = xhh.main([
                    "--data-dir", str(root), "snapshot", "resume", "public",
                ], client_factory=lambda config: self.fail(
                    "offline replay requested a client"))
            value = json.loads(output.getvalue())
            saved = Store(root).get("0", "public")

        self.assertEqual(code, 0, value)
        self.assertTrue(value["ok"])
        self.assertEqual(value["data"]["body"]["text"], "saved body")
        self.assertTrue(value["coverage"]["replayed_delivery"])
        self.assertIsNone(saved["pending_response"])

    def test_account_status_online_uses_web_only_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(accounts, "_dpapi",
                              side_effect=lambda value, decrypt=False: value), \
                    patch.object(accounts, "_restrict_directory",
                                 return_value=None), \
                    patch.object(
                        accounts.AccountStore,
                        "_locked",
                        new=lambda self: contextlib.nullcontext(),
                    ):
                store = accounts.AccountStore(root)
                store.add("web-only", identity="101")
                account = store.get("web-only")
                store.save_login(
                    "web-only", pkey="web-session", identity="101",
                    expected_revision=account.revision,
                    protocol_mode="web",
                )
                store.configure("web-only", {"protocol_mode": "app"})
                seen = []

                class Client:
                    def __init__(self, config):
                        seen.append(config.protocol_mode)

                    def account_info(self):
                        return {"userid": "101"}

                output = io.StringIO()
                with patch.object(cli, "XhhClient", Client), \
                        contextlib.redirect_stdout(output):
                    code = cli.main([
                        "--data-dir", str(root),
                        "account", "status", "web-only", "--online",
                    ])
                value = json.loads(output.getvalue())

        self.assertEqual(code, 0, value)
        self.assertEqual(value["protocol_mode"], "web")
        self.assertEqual(value["state"], "verified")
        self.assertEqual(seen, ["web"])

    def test_unknown_platform_status_is_blocked_without_resume(self):
        with self.assertRaises(XhhAPIError) as raised:
            Transport._check_status({"status": "future_non_success"})
        self.assertFalse(raised.exception.retry_allowed)
        projected = project_coverage({
            "stop_reason": "platform_error",
            "resume": "unsafe-token",
            "error": raised.exception.public(),
        })
        self.assertEqual(projected["status"], "blocked")
        self.assertIsNone(projected["resume"])


class ConfigurationBoundaryTests(unittest.TestCase):
    def test_explicit_html_validates_an_explicit_account_alias(self):
        class MissingAccountStore:
            def __init__(self, root):
                self.root = root

            def get(self, alias):
                raise XhhConfigError("Account not found")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = io.StringIO()
            requests = []
            with patch("xhh.AccountStore", MissingAccountStore), \
                    patch("browser.read_http", side_effect=lambda request, *args,
                          **kwargs: requests.append(request)
                          or b"<article>body</article>"), \
                    contextlib.redirect_stdout(output):
                code = xhh.main([
                    "--data-dir", str(root),
                    "--account", "missing",
                    "post", "123", "--reader", "html", "--requests", "1",
                ])
            value = json.loads(output.getvalue())

        self.assertEqual(code, 1, value)
        self.assertEqual(value["error"]["code"], "configuration_error")
        self.assertEqual(requests, [])

    def test_legacy_app_config_explicit_website_reports_web_session_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "app.json"
            config.write_text(json.dumps({
                "pkey": "app-session",
                "heybox_id": "101",
                "protocol_mode": "app",
            }), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = xhh.main([
                    "--config", str(config),
                    "--data-dir", str(root),
                    "post", "123", "--reader", "website",
                ], client_factory=lambda config: self.fail(
                    "missing Web session created a client"))
            value = json.loads(output.getvalue())

        self.assertEqual(code, 1, value)
        self.assertEqual(value["error"]["code"], "web_session_missing")


if __name__ == "__main__":
    unittest.main()
