import json
import sys
import tempfile
import time
import unittest
import urllib.request
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import corpus as corpus_module
import xhh
from browser import Browser, ReaderSpec
from corpus import CorpusBudget, stop_for_error
from corpus_model import CorpusManifest
from corpus_store import CorpusLocked, CorpusPaused, CorpusStore
from documents import document
from paging import ReadLimit
from xhh_sdk.config import XhhConfig
from xhh_sdk.exceptions import XhhCaptchaError, XhhConfigError
from xhh_sdk.transport import Transport


MANIFEST = {
    "schema_version": 1,
    "run_id": "budget-run",
    "account": {"alias": "fixture", "uid": "101"},
    "reader": {"protocol": "app", "page_size": 5},
    "limits": {"requests": 2, "seconds": 30, "unique_records": 10},
    "queries": [{
        "query_id": "feed-one",
        "kind": "feed",
        "target": {"topic_id": "7214"},
        "filters": {},
        "limits": {"pages": 2, "new_records": 5},
    }],
}


class Client:
    def __init__(self):
        self.config = XhhConfig(
            pkey="fixture", heybox_id="101", protocol_mode="app")
        self.transport = self
        self.calls = []

    def signed_request(self, path, query=None):
        self.calls.append((path, query))
        return {"result": {"links": [], "has_more": False}}


class CorpusBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = CorpusStore(self.root)
        self.store.plan(CorpusManifest.from_value(MANIFEST))
        self.client = Client()

    def tearDown(self):
        self.temp.cleanup()

    def test_request_ceiling_is_shared_across_roles_and_persists(self):
        lease = self.store.acquire_lease(
            "budget-run", "worker-one", max_requests=2)
        budget = CorpusBudget(
            self.store, lease, role="identity",
            protocol=self.client.config.protocol_mode)
        budget.call(self.client, lambda: {"userid": "101"})
        page_budget = CorpusBudget(
            self.store, lease, query_id="feed-one", frame_key="frame-one")
        page_budget.call(
            self.client,
            lambda: self.client.signed_request("/bbs/app/topic/feeds"),
        )
        calls = len(self.client.calls)
        with self.assertRaises(ReadLimit):
            CorpusBudget(
                self.store, lease, role="reader_fallback",
                protocol=self.client.config.protocol_mode,
            ).call(self.client, lambda: {"fallback": True})
        self.assertEqual(len(self.client.calls), calls)
        self.store.release_lease(lease)
        status = self.store.status("budget-run")
        self.assertEqual(status["budget"]["requests"]["used"], 2)

        with self.assertRaises(CorpusPaused):
            self.store.acquire_lease(
                "budget-run", "worker-two", max_requests=2)
        self.assertEqual(len(self.client.calls), calls)

    def test_one_attempt_cannot_dispatch_two_wire_requests(self):
        value = json.loads(json.dumps(MANIFEST))
        value["run_id"] = "wire-budget-run"
        value["limits"]["requests"] = 3
        manifest = CorpusManifest.from_value(value)
        store = CorpusStore(self.root)
        store.plan(manifest)
        lease = store.acquire_lease(
            "wire-budget-run", "wire-worker", max_requests=3)

        transport = Transport(self.client.config)
        transport.signer = object()

        class TransportClient:
            def __init__(self, source):
                self.config = source.config
                self.transport = source

        client = TransportClient(transport)
        wire_calls = []

        def read_http(request, timeout, *, wire_counter, **kwargs):
            wire_calls.append(request.full_url)
            wire_counter(1)
            return b'{"status":"ok"}'

        def two_wire_identity():
            request = urllib.request.Request(
                "https://api.xiaoheihe.cn/account/info", method="GET")
            transport._open(request)
            transport._open(request)
            return {"userid": "101"}

        def one_wire_identity():
            request = urllib.request.Request(
                "https://api.xiaoheihe.cn/account/info/", method="GET")
            transport._open(request)
            return {"userid": "101"}

        with patch("paging.read_http", side_effect=read_http):
            with self.assertRaises(ReadLimit) as stopped:
                CorpusBudget(
                    store, lease, role="identity").call(
                        client, two_wire_identity)
            self.assertEqual(
                store.status("wire-budget-run")["budget"]["requests"],
                {"used": 1, "limit": 3},
            )
            self.assertEqual(
                CorpusBudget(
                    store, lease, role="identity").call(
                        client, one_wire_identity),
                {"userid": "101"},
            )
            self.assertEqual(
                CorpusBudget(
                    store, lease, role="identity").call(
                        client, one_wire_identity),
                {"userid": "101"},
            )
            before_exhausted = len(wire_calls)
            with self.assertRaises(ReadLimit):
                CorpusBudget(
                    store, lease, role="identity").call(
                        client, one_wire_identity)

        self.assertEqual(stopped.exception.reason, "request_budget")
        self.assertEqual(len(wire_calls), 3)
        self.assertEqual(before_exhausted, 3)
        self.assertEqual(
            store.status("wire-budget-run")["budget"]["requests"],
            {"used": 3, "limit": 3},
        )
        store.release_lease(lease)

    def test_live_foreign_lease_is_rejected(self):
        self.store.acquire_lease("budget-run", "worker-one")
        with self.assertRaises(CorpusLocked):
            self.store.acquire_lease("budget-run", "worker-two")

    def test_lease_durations_must_be_finite_bounded_numbers(self):
        invalid_seconds = (
            True, float("nan"), float("inf"), float("-inf"),
            1e308, 10 ** 1000,
        )
        for invalid in invalid_seconds:
            with self.subTest(argument="lease_seconds", invalid=invalid):
                with self.assertRaises(XhhConfigError):
                    self.store.acquire_lease(
                        "budget-run", "invalid-lease",
                        lease_seconds=invalid)
            with self.subTest(argument="max_seconds", invalid=invalid):
                with self.assertRaises(XhhConfigError):
                    self.store.acquire_lease(
                        "budget-run", "invalid-max-seconds",
                        max_seconds=invalid)

    def test_invocation_caps_require_bounded_integers(self):
        invalid_values = (True, -1, 1_000_001, 10 ** 1000)
        for argument in ("max_requests", "max_new_records"):
            for invalid in invalid_values:
                with self.subTest(argument=argument, invalid=invalid):
                    with self.assertRaises(XhhConfigError):
                        self.store.acquire_lease(
                            "budget-run", "invalid-cap",
                            **{argument: invalid})

    def test_time_budget_does_not_reset_between_calls(self):
        lease = self.store.acquire_lease(
            "budget-run", "time-worker", max_seconds=1)
        lease["time_started_monotonic"] -= 2
        budget = CorpusBudget(self.store, lease, role="identity")
        with self.assertRaises(ReadLimit) as stopped:
            budget.call(self.client, lambda: {"should": "not run"})
        self.assertEqual(stopped.exception.reason, "time_budget")
        self.assertEqual(self.client.calls, [])

    def test_attempt_reservation_consumes_the_same_invocation_deadline(self):
        lease = self.store.acquire_lease(
            "budget-run", "slow-reserve", max_seconds=1)
        lease["time_started_monotonic"] = 0.0
        lease["time_limit_ms"] = 1000
        clock = {"now": 0.0}
        original_reserve = self.store.reserve_attempt

        def delayed_reserve(*args, **kwargs):
            token = original_reserve(*args, **kwargs)
            clock["now"] = 1.1
            return token

        with patch.object(
                corpus_module.time, "monotonic",
                side_effect=lambda: clock["now"]), patch.object(
                    self.store, "reserve_attempt",
                    side_effect=delayed_reserve):
            budget = CorpusBudget(self.store, lease, role="identity")
            with self.assertRaises(ReadLimit) as stopped:
                budget.call(self.client, lambda: self.client.calls.append(1))

        self.assertEqual(stopped.exception.reason, "time_budget")
        self.assertEqual(self.client.calls, [])
        with closing(
                __import__("sqlite3").connect(self.store.path)) as connection:
            status = connection.execute(
                "SELECT status FROM corpus_attempts WHERE run_id=?",
                ("budget-run",),
            ).fetchone()[0]
        self.assertEqual(status, "failed")

    def test_captcha_response_received_after_deadline_stays_blocked(self):
        lease = self.store.acquire_lease(
            "budget-run", "captcha-deadline", max_seconds=1)
        lease["time_started_monotonic"] = 0.0
        lease["time_limit_ms"] = 1000
        clock = {"now": 0.0}
        transport = Transport(self.client.config)
        transport.signer = object()

        class TransportClient:
            def __init__(self, value):
                self.config = value.config
                self.transport = value

        client = TransportClient(transport)

        def after_deadline(*args, **kwargs):
            clock["now"] = 1.1
            return b'{"status":"show_captcha","msg":"verify"}'

        def read_page():
            request = urllib.request.Request(
                "https://api.xiaoheihe.cn/fixture", method="GET")
            body = transport._open(request)
            data = transport._decode(body)
            return transport._checked_response("/fixture", data)

        with patch.object(
                corpus_module.time, "monotonic",
                side_effect=lambda: clock["now"]), patch(
                    "paging.read_http", side_effect=after_deadline):
            with self.assertRaises(XhhCaptchaError) as raised:
                CorpusBudget(
                    self.store, lease, role="identity").call(
                        client, read_page)

        decision = stop_for_error(raised.exception)
        self.assertEqual(decision.next_state, "blocked_human")
        self.assertEqual(decision.code, "captcha_required")
        self.assertFalse(decision.retry_allowed)

    def test_expired_reservation_remains_charged_and_abandoned(self):
        lease = self.store.acquire_lease(
            "budget-run", "worker-one", max_requests=2, max_seconds=4)
        self.store.reserve_attempt(
            lease,
            query_id="feed-one",
            frame_key="abandoned-frame",
            role="page",
            protocol="app",
            invocation_limit=2,
        )
        with closing(
                __import__("sqlite3").connect(self.store.path)) as connection:
            connection.execute(
                "UPDATE corpus_runs SET lease_expires_at=0 WHERE run_id=?",
                ("budget-run",),
            )
            connection.commit()
        restarted = self.store.acquire_lease(
            "budget-run", "worker-two", max_requests=1, max_seconds=2)
        status = self.store.status("budget-run")
        self.assertEqual(status["budget"]["requests"]["used"], 1)
        self.assertLess(status["budget"]["seconds"]["used"], 4)
        with closing(
                __import__("sqlite3").connect(self.store.path)) as connection:
            attempt = connection.execute(
                "SELECT status FROM corpus_attempts "
                "WHERE run_id=? AND frame_key='abandoned-frame'",
                ("budget-run",),
            ).fetchone()
        self.assertEqual(attempt[0], "abandoned")
        self.assertEqual(restarted["request_limit"], 1)

    def test_expired_lease_without_attempts_preserves_time_quota(self):
        lease = self.store.acquire_lease(
            "budget-run", "idle-worker", max_seconds=4)
        with closing(
                __import__("sqlite3").connect(self.store.path)) as connection:
            connection.execute(
                "UPDATE corpus_runs SET lease_expires_at=0 WHERE run_id=?",
                ("budget-run",),
            )
            connection.commit()

        restarted = self.store.acquire_lease(
            "budget-run", "next-worker", max_seconds=4)
        status = self.store.status("budget-run")
        self.assertEqual(status["budget"]["seconds"]["used"], 0)
        self.assertEqual(restarted["time_limit_ms"], 4000)

    def test_reader_fallback_cannot_exceed_the_shared_request_gate(self):
        lease = self.store.acquire_lease(
            "budget-run", "fallback-budget", max_requests=1)
        calls = {"app": 0, "web": 0}

        def app_reader(args, budget):
            def read():
                calls["app"] += 1
                return {"reader": "app"}

            budget.call(self.client, read)
            value = document(
                {"linkid": "123", "text": "partial body"},
                {
                    "route": "/fixture/app",
                    "protocol": "app",
                    "observed_at": 1.0,
                    "configured_uid": "101",
                    "identity_verified": True,
                    "session_revision": "app-one",
                    "reader": "native",
                },
            )
            return value, {"reader": "app"}

        def web_reader(args, budget):
            def read():
                calls["web"] += 1
                return {"reader": "web"}

            budget.call(self.client, read)
            raise AssertionError("fallback reader was allowed to run")

        browser = Browser(self.client)
        args = xhh.parser().parse_args([
            "post", "123", "--reader", "auto",
        ])
        data, scope = browser.post(
            args,
            CorpusBudget(self.store, lease),
            plan=[
                ReaderSpec("native", "app", "/fixture/app", app_reader),
                ReaderSpec("website", "web", "/fixture/web", web_reader),
            ],
        )

        self.assertEqual(calls, {"app": 1, "web": 0})
        self.assertEqual(data["reader_attempts"][0]["status"], "ok")
        self.assertEqual(
            data["reader_attempts"][1]["status"], "budget_stop")
        self.assertEqual(scope["status"], "partial")
        self.assertEqual(scope["stop_reason"], "request_budget")
        self.assertEqual(
            self.store.status("budget-run")["budget"]["requests"]["used"],
            1,
        )
        self.store.release_lease(lease)

    def test_public_html_reader_uses_the_same_durable_attempt_gate(self):
        lease = self.store.acquire_lease(
            "budget-run", "public-reader", max_requests=1)

        def public_read(remaining, wire_limit, wire_counter):
            self.assertGreater(remaining, 0)
            self.assertEqual(wire_limit, 1)
            wire_counter(1)
            return b"<article>body</article>"

        body = CorpusBudget(
            self.store,
            lease,
            role="reader_fallback",
            protocol="public",
        ).call_wire(public_read)

        self.assertEqual(body, b"<article>body</article>")
        self.assertEqual(
            self.store.status("budget-run")["budget"]["requests"],
            {"used": 1, "limit": 2},
        )
        with closing(
                __import__("sqlite3").connect(self.store.path)) as connection:
            protocol = connection.execute(
                "SELECT protocol FROM corpus_attempts WHERE run_id=?",
                ("budget-run",),
            ).fetchone()[0]
        self.assertEqual(protocol, "public")
        self.store.release_lease(lease)

    def test_captcha_reader_error_does_not_attempt_a_fallback(self):
        lease = self.store.acquire_lease(
            "budget-run", "fallback-captcha")
        calls = {"app": 0, "web": 0}

        def app_reader(args, budget):
            def read():
                calls["app"] += 1
                raise XhhCaptchaError("platform requires captcha")

            return budget.call(self.client, read)

        def web_reader(args, budget):
            calls["web"] += 1
            raise AssertionError("captcha fell through to the web reader")

        browser = Browser(self.client)
        args = xhh.parser().parse_args([
            "post", "123", "--reader", "auto",
        ])
        with self.assertRaises(XhhCaptchaError):
            browser.post(
                args,
                CorpusBudget(self.store, lease),
                plan=[
                    ReaderSpec(
                        "native", "app", "/fixture/app", app_reader),
                    ReaderSpec(
                        "website", "web", "/fixture/web", web_reader),
                ],
            )

        self.assertEqual(calls, {"app": 1, "web": 0})
        self.assertEqual(
            self.store.status("budget-run")["budget"]["requests"]["used"],
            1,
        )
        self.store.release_lease(lease)


if __name__ == "__main__":
    unittest.main()
