import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import xhh
from browser import Browser
from corpus import CorpusBudget, CorpusRunner, stop_for_error
from corpus_model import (
    CorpusManifest,
    PageEvidence,
    PageResult,
    Position,
    ProviderItem,
    frame_key,
    sha256_value,
)
from corpus_store import CorpusConflict, CorpusLocked, CorpusStore
from paging import coverage
from xhh_sdk.config import XhhConfig
from xhh_sdk.exceptions import (
    XhhAPIError,
    XhhAuthError,
    XhhCaptchaError,
    XhhRateLimitError,
    XhhSignerError,
)


def manifest(*query_ids, requests=10, new_records=20, pages=2):
    return {
        "schema_version": 1,
        "run_id": "runner-run",
        "account": {"alias": "fixture", "uid": "101"},
        "reader": {"protocol": "app", "page_size": 5},
        "limits": {
            "requests": requests,
            "seconds": 30,
            "unique_records": new_records,
        },
        "queries": [
            {
                "query_id": query_id,
                "kind": "feed",
                "target": {"topic_id": str(100 + index)},
                "filters": {},
                "limits": {"pages": pages, "new_records": 10},
            }
            for index, query_id in enumerate(query_ids, 1)
        ],
    }


class Client:
    def __init__(self, handler):
        self.config = XhhConfig(
            pkey="fixture", heybox_id="101", protocol_mode="app")
        self.transport = self
        self.handler = handler
        self.calls = []
        self.identity_calls = 0

    def account_info(self):
        self.identity_calls += 1
        return {"userid": self.config.heybox_id}

    def signed_request(self, path, query=None):
        self.calls.append((path, query))
        value = self.handler(path, query)
        if isinstance(value, BaseException):
            raise value
        return value


def feed(ids, *, more=False):
    return {
        "result": {
            "links": [
                {"linkid": str(identifier), "title": f"post-{identifier}"}
                for identifier in ids
            ],
            "has_more": more,
        },
    }


class CorpusRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, value):
        parsed = CorpusManifest.from_value(value)
        store = CorpusStore(self.root)
        store.plan(parsed)
        return parsed, store

    def run_events(self, runner, store):
        events = []
        generator = runner.run()
        while True:
            try:
                event = next(generator)
            except StopIteration as stopped:
                final = stopped.value
                break
            events.append(event)
            if event.get("event") == "delivery":
                store.acknowledge_delivery(
                    "runner-run", event["revision"], lease=runner.lease)
        return events, final

    def test_cross_query_deduplication_uses_one_entity_and_memberships(self):
        value = manifest("one", "two")
        parsed, store = self.prepare(value)

        def handler(path, query):
            if query["topic_id"] == "101":
                return feed(["1", "2"])
            return feed(["2", "3"])

        client = Client(handler)
        lease = store.acquire_lease("runner-run", "dedupe")
        runner = CorpusRunner(
            store,
            parsed,
            lease,
            parser=xhh.parser,
            client_loader=lambda: client,
            browser_factory=Browser,
        )
        events, final = self.run_events(runner, store)
        store.release_lease(lease)

        self.assertEqual(client.identity_calls, 1)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(final["run"]["state"], "complete")
        self.assertEqual(len(events), 2)
        status = store.status("runner-run")
        self.assertEqual(status["budget"]["unique_records"]["used"], 3)
        with closing(__import__("sqlite3").connect(store.path)) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM corpus_entities").fetchone()[0], 3)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM corpus_memberships").fetchone()[0], 4)
            self.assertEqual(connection.execute(
                """SELECT COUNT(*) FROM corpus_memberships
                   WHERE entity_key='post:2'""").fetchone()[0], 2)
            evidence = json.loads(connection.execute(
                "SELECT evidence_json FROM corpus_frames LIMIT 1"
            ).fetchone()[0])
        self.assertTrue(evidence[0]["identity_verified"])

    def test_captcha_on_query_two_stops_query_three_and_requires_ack(self):
        parsed, store = self.prepare(manifest("one", "two", "three"))

        def handler(path, query):
            if query["topic_id"] == "101":
                return feed(["1"])
            return XhhCaptchaError("platform requires captcha")

        client = Client(handler)
        lease = store.acquire_lease("runner-run", "captcha")
        runner = CorpusRunner(
            store,
            parsed,
            lease,
            parser=xhh.parser,
            client_loader=lambda: client,
            browser_factory=Browser,
        )
        events, final = self.run_events(runner, store)
        store.release_lease(lease)

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.identity_calls, 1)
        self.assertEqual(final["run"]["state"], "blocked_human")
        self.assertEqual(final["queries"][1]["state"], "blocked")
        self.assertEqual(final["queries"][2]["state"], "pending")
        self.assertIsNone(store.status("runner-run")["pending_delivery"])

        output = io.StringIO()
        with patch(
                "corpus_cli._load_bound_client",
                side_effect=AssertionError("blocked resume loaded credentials"),
        ), contextlib.redirect_stdout(output):
            code = xhh.main([
                "--data-dir", str(self.root), "corpus", "resume",
                "runner-run",
            ])
        blocked = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(blocked["coverage"]["status"], "blocked_human")
        self.assertIsNone(blocked["coverage"]["continuation"])
        self.assertEqual(
            blocked["error"]["code"],
            "corpus_block_acknowledgement_required",
        )

    def test_identity_and_pages_share_the_request_ceiling(self):
        parsed, store = self.prepare(manifest(
            "one", "two", requests=2))
        client = Client(lambda path, query: feed(["1"]))
        lease = store.acquire_lease(
            "runner-run", "request-cap", max_requests=2)
        runner = CorpusRunner(
            store,
            parsed,
            lease,
            parser=xhh.parser,
            client_loader=lambda: client,
            browser_factory=Browser,
        )
        events, final = self.run_events(runner, store)
        store.release_lease(lease)

        self.assertEqual(client.identity_calls, 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(final["run"]["state"], "paused_budget")
        self.assertEqual(final["queries"][1]["state"], "pending")

    def test_budget_pause_after_page_does_not_invent_a_remainder(self):
        parsed, store = self.prepare(manifest(
            "one", requests=2, pages=2))
        client = Client(lambda path, query: feed(["1"], more=True))
        lease = store.acquire_lease(
            "runner-run", "page-budget", max_requests=2)
        events, final = self.run_events(CorpusRunner(
            store,
            parsed,
            lease,
            parser=xhh.parser,
            client_loader=lambda: client,
            browser_factory=Browser,
        ), store)
        store.release_lease(lease)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(events), 2)
        self.assertEqual(final["run"]["state"], "paused_budget")
        self.assertEqual(final["queries"][0]["state"], "running")
        self.assertEqual(final["queries"][0]["pending_remainder_count"], 0)

    def test_query_quota_reached_is_terminal_even_with_deferred_rows(self):
        value = manifest("one", pages=2)
        value["queries"][0]["limits"]["new_records"] = 1
        parsed, store = self.prepare(value)

        def handler(path, query):
            return feed(["1", "2", "3"], more=True)

        client = Client(handler)
        lease = store.acquire_lease(
            "runner-run", "query-quota", max_new_records=5)
        events, final = self.run_events(CorpusRunner(
            store,
            parsed,
            lease,
            parser=xhh.parser,
            client_loader=lambda: client,
            browser_factory=Browser,
        ), store)
        store.release_lease(lease)

        self.assertEqual(len(events), 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(final["run"]["state"], "complete")
        self.assertEqual(final["queries"][0]["state"], "quota_reached")
        self.assertEqual(
            final["queries"][0]["pending_remainder_count"], 0)
        with self.assertRaises(CorpusLocked):
            store.acquire_lease("runner-run", "must-not-reopen")

    def test_mixed_feed_search_and_hashtag_queries_use_existing_routes(self):
        value = manifest("feed", "search", "hashtag")
        value["queries"][1] = {
            "query_id": "search",
            "kind": "search",
            "target": {"query": "alpha"},
            "filters": {"order": "newest"},
            "limits": {"pages": 1, "new_records": 10},
        }
        value["queries"][2] = {
            "query_id": "hashtag",
            "kind": "hashtag_links",
            "target": {"hashtag_id": "23455", "hashtag_name": "存钱"},
            "filters": {},
            "limits": {"pages": 1, "new_records": 10},
        }
        parsed, store = self.prepare(value)

        def handler(path, query):
            if path.endswith("/topic/feeds"):
                return feed(["1"])
            if path.endswith("/api/general/search/v1"):
                return {"result": {"items": [{
                    "type": "link",
                    "info": {"linkid": "2"},
                }]}}
            if path.endswith("/hashtag/link/list"):
                return {"result": {"links": [{"linkid": "3"}]}}
            raise AssertionError(path)

        client = Client(handler)
        lease = store.acquire_lease("runner-run", "routes")
        events, final = self.run_events(CorpusRunner(
            store,
            parsed,
            lease,
            parser=xhh.parser,
            client_loader=lambda: client,
            browser_factory=Browser,
        ), store)
        store.release_lease(lease)
        self.assertEqual(final["run"]["state"], "complete")
        self.assertEqual(len(events), 3)
        self.assertEqual(
            [path for path, _ in client.calls],
            [
                "/bbs/app/topic/feeds",
                "/bbs/app/api/general/search/v1",
                "/bbs/app/hashtag/link/list",
            ],
        )

    def test_fixed_unique_quota_pauses_without_another_wire_request(self):
        value = manifest("one", "two", new_records=1)
        value["queries"][1]["limits"]["new_records"] = 10
        parsed, store = self.prepare(value)
        client = Client(lambda path, query: feed([
            "1" if query["topic_id"] == "101" else "2",
        ]))
        lease = store.acquire_lease("runner-run", "fixed-quota")
        events, final = self.run_events(CorpusRunner(
            store,
            parsed,
            lease,
            parser=xhh.parser,
            client_loader=lambda: client,
            browser_factory=Browser,
        ), store)
        store.release_lease(lease)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(final["run"]["state"], "paused_budget")
        self.assertEqual(
            final["budget"]["unique_records"],
            {"used": 1, "limit": 1},
        )
        self.assertEqual(len(events), 2)

        resumed = store.acquire_lease(
            "runner-run", "fixed-quota-resume",
            max_requests=10, max_new_records=100)
        resumed_client = Client(lambda *args: self.fail("fixed quota sent request"))
        resumed_events, resumed_final = self.run_events(CorpusRunner(
            store,
            parsed,
            resumed,
            parser=xhh.parser,
            client_loader=lambda: resumed_client,
            browser_factory=Browser,
        ), store)
        store.release_lease(resumed)
        self.assertEqual(resumed_client.calls, [])
        self.assertEqual(resumed_final["budget"]["unique_records"]["limit"], 1)
        self.assertEqual(resumed_final["run"]["state"], "paused_budget")
        self.assertEqual(len(resumed_events), 1)

    def test_manifest_limits_override_tampered_query_quota_rows(self):
        value = manifest("one", pages=1)
        value["queries"][0]["limits"]["new_records"] = 1
        parsed, store = self.prepare(value)
        with closing(sqlite3.connect(store.path)) as connection:
            connection.execute(
                """UPDATE corpus_queries
                   SET page_limit=99,new_record_limit=99
                   WHERE run_id=? AND query_id=?""",
                ("runner-run", "one"),
            )
            connection.commit()

        def handler(path, query):
            if query["offset"] == "0":
                return feed(["1", "2"], more=True)
            return feed([], more=False)

        client = Client(handler)
        lease = store.acquire_lease("runner-run", "tampered-quota")
        events, final = self.run_events(CorpusRunner(
            store,
            parsed,
            lease,
            parser=xhh.parser,
            client_loader=lambda: client,
            browser_factory=Browser,
        ), store)
        store.release_lease(lease)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(final["queries"][0]["page_limit"], 1)
        self.assertEqual(final["queries"][0]["new_record_limit"], 1)
        self.assertEqual(final["queries"][0]["pages_used"], 1)
        self.assertEqual(final["queries"][0]["new_records_used"], 1)
        self.assertEqual(final["queries"][0]["state"], "quota_reached")

    def test_remainder_is_delivered_before_identity_or_next_page(self):
        value = manifest("one", pages=2)
        value["queries"][0]["limits"]["new_records"] = 10
        parsed, store = self.prepare(value)

        def handler(path, query):
            if query["offset"] == "0":
                return feed(["1", "2", "3", "4", "5"], more=True)
            return feed(["6"])

        client = Client(handler)
        first_lease = store.acquire_lease(
            "runner-run", "first", max_new_records=2)
        first = CorpusRunner(
            store,
            parsed,
            first_lease,
            parser=xhh.parser,
            client_loader=lambda: client,
            browser_factory=Browser,
        )
        first_events, first_final = self.run_events(first, store)
        store.release_lease(first_lease)
        self.assertEqual(first_final["run"]["state"], "paused_budget")
        self.assertEqual(len(first_events), 1)
        self.assertEqual(len(client.calls), 1)

        second_lease = store.acquire_lease(
            "runner-run", "second", max_new_records=5)
        second = CorpusRunner(
            store,
            parsed,
            second_lease,
            parser=xhh.parser,
            client_loader=lambda: client,
            browser_factory=Browser,
        )
        generator = second.run()
        tail = next(generator)
        self.assertEqual(tail["event"], "delivery")
        self.assertEqual(tail["counts"]["provider"], 0)
        self.assertEqual(tail["counts"]["new"], 3)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.identity_calls, 1)
        store.acknowledge_delivery("runner-run", tail["revision"])

        page_two = next(generator)
        self.assertEqual(page_two["event"], "delivery")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.identity_calls, 2)
        store.acknowledge_delivery("runner-run", page_two["revision"])
        with self.assertRaises(StopIteration) as stopped:
            next(generator)
        self.assertEqual(stopped.exception.value["run"]["state"], "complete")
        store.release_lease(second_lease)

    def test_exhausted_network_budget_still_drains_a_local_remainder(self):
        value = manifest("one", requests=1, pages=2)
        value["queries"][0]["limits"]["new_records"] = 10
        parsed, store = self.prepare(value)
        query = parsed.queries[0]
        position = Position("offset", 0)
        key = frame_key(query, position, parsed.reader)

        first = store.acquire_lease(
            "runner-run", "page-only", max_requests=1,
            max_new_records=2)
        token = store.reserve_attempt(
            first,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=first["request_limit"],
        )
        contents = [
            {"id": str(index), "title": f"post-{index}"}
            for index in range(1, 6)
        ]
        page = PageResult(
            items=tuple(
                ProviderItem(
                    ordinal=index,
                    content=content,
                    entity_key=f"post:{content['id']}",
                    digest=sha256_value(content),
                    eligible=True,
                )
                for index, content in enumerate(contents)
            ),
            coverage=coverage(
                {"has_more": True}, count=5, position=0),
            evidence=(PageEvidence(
                "/fixture", "app", 1.0, "101", True, "session"),),
            current_position=position,
            next_position=Position("offset", 5),
        )
        delivery = store.commit_page(first, query.query_id, token, page)
        store.acknowledge_delivery(
            "runner-run", delivery["revision"], lease=first)
        store.release_lease(first)

        resumed = store.acquire_lease(
            "runner-run", "local-only", max_requests=0, max_seconds=0,
            max_new_records=5)
        runner = CorpusRunner(
            store,
            parsed,
            resumed,
            parser=xhh.parser,
            client_loader=lambda: self.fail(
                "local remainder loaded a client"),
            browser_factory=Browser,
        )
        generator = runner.run()
        tail = next(generator)
        self.assertEqual(tail["event"], "delivery")
        self.assertEqual(tail["counts"]["provider"], 0)
        self.assertEqual(tail["counts"]["new"], 3)
        store.acknowledge_delivery(
            "runner-run", tail["revision"], lease=resumed)
        paused = next(generator)
        self.assertEqual(paused["event"], "status")
        self.assertEqual(
            paused["status"]["run"]["state"], "paused_budget")
        with self.assertRaises(StopIteration):
            next(generator)
        store.release_lease(resumed)

    def test_exact_final_remainder_quota_pauses_before_loading_a_client(self):
        value = manifest("one", "two", requests=3, pages=2)
        value["queries"][0]["limits"]["new_records"] = 5
        parsed, store = self.prepare(value)
        query = parsed.queries[0]
        position = Position("offset", 0)
        key = frame_key(query, position, parsed.reader)

        first = store.acquire_lease(
            "runner-run", "partial-page", max_requests=1,
            max_new_records=2)
        token = store.reserve_attempt(
            first,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=first["request_limit"],
        )
        contents = [
            {"id": str(index), "title": f"post-{index}"}
            for index in range(1, 6)
        ]
        page = PageResult(
            items=tuple(
                ProviderItem(
                    ordinal=index,
                    content=content,
                    entity_key=f"post:{content['id']}",
                    digest=sha256_value(content),
                    eligible=True,
                )
                for index, content in enumerate(contents)
            ),
            coverage=coverage(
                {"has_more": True}, count=5, position=0),
            evidence=(PageEvidence(
                "/fixture", "app", 1.0, "101", True, "session"),),
            current_position=position,
            next_position=Position("offset", 5),
        )
        delivery = store.commit_page(first, query.query_id, token, page)
        store.acknowledge_delivery(
            "runner-run", delivery["revision"], lease=first)
        store.release_lease(first)

        resumed = store.acquire_lease(
            "runner-run", "exact-remainder", max_requests=3,
            max_new_records=3)
        runner = CorpusRunner(
            store,
            parsed,
            resumed,
            parser=xhh.parser,
            client_loader=lambda: self.fail(
                "an exhausted invocation loaded a client"),
            browser_factory=Browser,
        )
        events, final = self.run_events(runner, store)
        store.release_lease(resumed)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "delivery")
        self.assertEqual(events[0]["counts"]["new"], 3)
        self.assertEqual(events[1]["event"], "status")
        self.assertEqual(events[1]["error"]["code"], "new_record_budget")
        self.assertEqual(final["run"]["state"], "paused_budget")
        self.assertEqual(final["queries"][0]["state"], "quota_reached")
        self.assertEqual(final["queries"][0]["pending_remainder_count"], 0)
        self.assertEqual(final["queries"][1]["state"], "pending")

    def test_fresh_process_replays_pending_delivery_without_clients(self):
        value = manifest("one")
        parsed, store = self.prepare(value)
        script = textwrap.dedent("""
            import sys
            from pathlib import Path
            sys.path.insert(0, sys.argv[2])
            from corpus_model import (
                PageEvidence, PageResult, Position, ProviderItem, frame_key,
                sha256_value,
            )
            from corpus_store import CorpusStore
            from paging import coverage

            root = Path(sys.argv[1])
            store = CorpusStore(root)
            manifest = store.manifest("runner-run")
            lease = store.acquire_lease(
                "runner-run", "child", lease_seconds=0.1, max_requests=2)
            query = manifest.queries[0]
            position = Position("offset", 0)
            key = frame_key(query, position, manifest.reader)
            token = store.reserve_attempt(
                lease, query_id=query.query_id, frame_key=key,
                role="page", protocol="app",
                invocation_limit=lease["request_limit"])
            store.settle_attempt(lease, token, success=True)
            content = {"id": "9", "title": "pending"}
            page = PageResult(
                items=(ProviderItem(
                    0, content, "post:9", sha256_value(content), True),),
                coverage=coverage(
                    {"has_more": False}, count=1, position=0),
                evidence=(PageEvidence(
                    "/fixture", "app", 1.0, "101", True, "session"),),
                current_position=position,
                next_position=None,
            )
            store.commit_page(lease, query.query_id, token, page)
            raise SystemExit(0)
        """)
        completed = subprocess.run(
            [
                sys.executable, "-I", "-c", script,
                str(self.root), str(Path(__file__).resolve().parents[1] / "scripts"),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsNotNone(store.pending_delivery("runner-run"))
        time.sleep(0.15)

        output = io.StringIO()
        with patch(
                "corpus_cli._load_bound_client",
                side_effect=AssertionError("replay loaded credentials"),
        ), patch(
                "xhh.AccountStore",
                side_effect=AssertionError("replay accessed accounts"),
        ), contextlib.redirect_stdout(output):
            code = xhh.main([
                "--data-dir", str(self.root), "corpus", "resume",
                "runner-run",
            ])
        replay = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(replay["coverage"]["replayed_delivery"])
        self.assertEqual(replay["data"]["new_items"][0]["id"], "9")
        self.assertIsNone(store.pending_delivery("runner-run"))
        self.assertEqual(
            store.status("runner-run")["run"]["state"], "complete")

    def test_expired_lease_recovers_staged_page_before_identity(self):
        parsed, store = self.prepare(manifest("one", requests=2))
        first = store.acquire_lease(
            "runner-run", "staged-first", max_requests=2)
        identity = Client(lambda *args: None)
        CorpusBudget(
            store, first, role="identity", protocol="app").call(
                identity, lambda: {"userid": "101"})
        query = parsed.queries[0]
        position = Position("offset", 0)
        key = frame_key(query, position, parsed.reader)
        token = store.reserve_attempt(
            first,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=first["request_limit"],
        )
        content = {"id": "9", "title": "staged"}
        page = PageResult(
            items=(ProviderItem(
                0, content, "post:9", sha256_value(content), True),),
            coverage=coverage(
                {"has_more": False}, count=1, position=0),
            evidence=(PageEvidence(
                "/fixture", "app", 1.0, "101", True, "session"),),
            current_position=position,
            next_position=None,
        )
        store.stage_page(first, token, page)
        with closing(sqlite3.connect(store.path)) as connection:
            connection.execute(
                "UPDATE corpus_runs SET lease_expires_at=0 WHERE run_id=?",
                ("runner-run",),
            )
            connection.commit()

        resumed = store.acquire_lease(
            "runner-run", "staged-second", max_requests=0)
        self.assertEqual(
            store.status("runner-run")["run"]["state"], "running")
        runner = CorpusRunner(
            store,
            parsed,
            resumed,
            parser=xhh.parser,
            client_loader=lambda: self.fail(
                "staged page recovery loaded a client"),
            browser_factory=Browser,
        )
        events, final = self.run_events(runner, store)
        store.release_lease(resumed)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["counts"]["new"], 1)
        self.assertEqual(final["run"]["state"], "complete")
        self.assertEqual(final["budget"]["requests"]["used"], 2)
        self.assertEqual(final["budget"]["pages"], 1)

    def test_local_frame_commit_failure_replays_staged_attempt(self):
        parsed, store = self.prepare(manifest("one"))
        lease = store.acquire_lease(
            "runner-run", "frame-failure", max_requests=1)
        query = parsed.queries[0]
        position = Position("offset", 0)
        key = frame_key(query, position, parsed.reader)
        token = store.reserve_attempt(
            lease,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=lease["request_limit"],
        )
        content = {"id": "9", "title": "staged"}
        page = PageResult(
            items=(ProviderItem(
                0, content, "post:9", sha256_value(content), True),),
            coverage=coverage(
                {"has_more": False}, count=1, position=0),
            evidence=(PageEvidence(
                "/fixture", "app", 1.0, "101", True, "session"),),
            current_position=position,
            next_position=None,
        )
        store.stage_page(lease, token, page)
        with patch.object(
                store, "_insert_delivery",
                side_effect=sqlite3.OperationalError("local commit failed")):
            with self.assertRaises(sqlite3.OperationalError):
                store.commit_page(lease, token=token)

        delivery = store.commit_page(lease, token=token)
        self.assertEqual(delivery["counts"]["new"], 1)
        self.assertEqual(
            store.status("runner-run")["budget"]["requests"]["used"], 1)

    def test_pending_replay_is_fenced_by_one_process_lease(self):
        parsed, store = self.prepare(manifest("one"))
        lease = store.acquire_lease("runner-run", "seed")
        query = parsed.queries[0]
        position = Position("offset", 0)
        key = frame_key(query, position, parsed.reader)
        token = store.reserve_attempt(
            lease,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=lease["request_limit"],
        )
        content = {"id": "9", "title": "pending"}
        page = PageResult(
            items=(ProviderItem(
                0, content, "post:9", sha256_value(content), True),),
            coverage=coverage(
                {"has_more": False}, count=1, position=0),
            evidence=(PageEvidence(
                "/fixture", "app", 1.0, "101", True, "session"),),
            current_position=position,
            next_position=None,
        )
        store.commit_page(lease, query.query_id, token, page)
        store.release_lease(lease)
        marker = self.root / "replay-claimed"
        script = textwrap.dedent("""
            import sys
            import time
            from pathlib import Path
            sys.path.insert(0, sys.argv[2])
            from corpus_store import CorpusLocked, CorpusStore

            store = CorpusStore(Path(sys.argv[1]))
            marker = Path(sys.argv[3])
            try:
                lease = store.acquire_lease(
                    "runner-run", sys.argv[4], lease_seconds=10)
            except CorpusLocked:
                print("locked")
                raise SystemExit(3)
            marker.write_text("claimed", encoding="utf-8")
            time.sleep(1)
            pending = store.pending_delivery("runner-run")
            print(pending["delivery_id"])
            store.acknowledge_delivery(
                "runner-run", pending["revision"], lease=lease)
            store.release_lease(lease)
        """)
        command = [
            sys.executable, "-I", "-c", script,
            str(self.root),
            str(Path(__file__).resolve().parents[1] / "scripts"),
            str(marker),
        ]
        first = subprocess.Popen(
            command + ["first"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not marker.exists():
            first.communicate(timeout=5)
            self.fail("no process acquired the pending replay lease")

        second = subprocess.run(
            command + ["second"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        first_stdout, first_stderr = first.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, first_stderr)
        self.assertTrue(first_stdout.strip())
        self.assertEqual(second.returncode, 3, second.stderr)
        self.assertEqual(second.stdout.strip(), "locked")
        self.assertIsNone(store.pending_delivery("runner-run"))

    def test_concurrent_public_resume_replays_checkpoint_once(self):
        parsed, store = self.prepare(manifest("one"))
        lease = store.acquire_lease("runner-run", "seed")
        query = parsed.queries[0]
        position = Position("offset", 0)
        key = frame_key(query, position, parsed.reader)
        token = store.reserve_attempt(
            lease,
            query_id=query.query_id,
            frame_key=key,
            role="page",
            protocol="app",
            invocation_limit=lease["request_limit"],
        )
        content = {"id": "9", "title": "pending"}
        page = PageResult(
            items=(ProviderItem(
                0, content, "post:9", sha256_value(content), True),),
            coverage=coverage(
                {"has_more": False}, count=1, position=0),
            evidence=(PageEvidence(
                "/fixture", "app", 1.0, "101", True, "session"),),
            current_position=position,
            next_position=None,
        )
        store.commit_page(lease, query.query_id, token, page)
        store.release_lease(lease)

        barrier = self.root / "pending-read"
        script = textwrap.dedent("""
            import sys
            import time
            from pathlib import Path
            sys.path.insert(0, sys.argv[2])
            import xhh
            from corpus_store import CorpusStore

            time.time_ns = lambda: 1
            root = Path(sys.argv[1])
            name = sys.argv[3]
            barrier = Path(sys.argv[4])
            original = CorpusStore.pending_delivery

            def gated_pending_delivery(store, run_id):
                pending = original(store, run_id)
                ready = barrier.with_name(barrier.name + f".{name}.ready")
                ready.write_text("ready", encoding="utf-8")
                deadline = time.monotonic() + 5
                while not barrier.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                return pending

            CorpusStore.pending_delivery = gated_pending_delivery
            raise SystemExit(xhh.main([
                "--data-dir", str(root), "corpus", "resume", "runner-run",
            ]))
        """)
        command = [
            sys.executable, "-I", "-c", script,
            str(self.root),
            str(Path(__file__).resolve().parents[1] / "scripts"),
        ]
        first = subprocess.Popen(
            command + ["first", str(barrier)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second = subprocess.Popen(
            command + ["second", str(barrier)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        first_ready = barrier.with_name(barrier.name + ".first.ready")
        second_ready = barrier.with_name(barrier.name + ".second.ready")
        deadline = time.monotonic() + 5
        while not (
            first_ready.exists() or second_ready.exists()
        ) and time.monotonic() < deadline:
            time.sleep(0.01)
        if not (first_ready.exists() or second_ready.exists()):
            first.communicate(timeout=5)
            second.communicate(timeout=5)
            self.fail("no public resume reached the pending checkpoint")
        barrier.write_text("continue", encoding="utf-8")
        first_stdout, first_stderr = first.communicate(timeout=10)
        second_stdout, second_stderr = second.communicate(timeout=10)

        self.assertEqual(
            first.returncode, 0, first_stderr + first_stdout)
        self.assertEqual(
            second.returncode, 0, second_stderr + second_stdout)
        outputs = [
            json.loads(line)
            for output in (first_stdout, second_stdout)
            for line in output.splitlines()
            if line
        ]
        replayed = [
            value for value in outputs
            if value.get("coverage", {}).get("replayed_delivery")
        ]
        self.assertEqual(len(replayed), 1)
        self.assertIsNone(store.pending_delivery("runner-run"))

    def test_public_run_command_emits_durable_page_and_final_status(self):
        parsed, store = self.prepare(manifest("one"))
        client = Client(lambda path, query: feed(["1"]))
        output = io.StringIO()
        with patch(
                "corpus_cli._load_bound_client",
                return_value=client,
        ), contextlib.redirect_stdout(output):
            code = xhh.main([
                "--data-dir", str(self.root), "corpus", "run",
                "runner-run",
            ], client_factory=lambda config: self.fail(
                "corpus bypassed the managed account loader"))
        values = [
            json.loads(line) for line in output.getvalue().splitlines()
            if line
        ]
        self.assertEqual(code, 0)
        self.assertEqual(len(values), 2)
        self.assertEqual(values[0]["data"]["new_items"][0]["id"], "1")
        self.assertEqual(values[1]["coverage"]["status"], "complete")

    def test_blocking_error_families_stop_the_whole_run(self):
        errors = [
            XhhRateLimitError("rate limited"),
            XhhAuthError("session expired"),
            XhhSignerError("signer unavailable"),
            XhhAPIError("policy rejected", retry_allowed=False),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                decision = stop_for_error(error)
                self.assertEqual(decision.next_state, "blocked_human")
                self.assertFalse(decision.retry_allowed)

    def test_retryable_http_api_error_pauses_and_resumes(self):
        parsed, store = self.prepare(manifest("one"))
        responses = [
            XhhAPIError(
                "HTTP 503",
                status="HTTP_503",
                retry_allowed=True,
            ),
            feed(["1"]),
        ]

        def handler(path, query):
            return responses.pop(0)

        first_client = Client(handler)
        first_lease = store.acquire_lease(
            "runner-run", "retryable-first", max_requests=3)
        first_events, first_final = self.run_events(CorpusRunner(
            store,
            parsed,
            first_lease,
            parser=xhh.parser,
            client_loader=lambda: first_client,
            browser_factory=Browser,
        ), store)
        store.release_lease(first_lease)

        self.assertEqual(first_events[-1]["event"], "status")
        self.assertEqual(first_events[-1]["error"]["code"], "platform_error")
        self.assertTrue(first_events[-1]["error"]["retry_allowed"])
        self.assertEqual(first_final["run"]["state"], "paused_retryable")
        self.assertEqual(
            first_final["queries"][0]["state"], "paused_retryable")

        resumed = store.acquire_lease(
            "runner-run", "retryable-second", max_requests=3)
        resumed_events, resumed_final = self.run_events(CorpusRunner(
            store,
            parsed,
            resumed,
            parser=xhh.parser,
            client_loader=lambda: first_client,
            browser_factory=Browser,
        ), store)
        store.release_lease(resumed)

        self.assertEqual(resumed_events[-1]["event"], "delivery")
        self.assertEqual(resumed_final["run"]["state"], "complete")
        self.assertEqual(resumed_final["queries"][0]["state"], "complete")


if __name__ == "__main__":
    unittest.main()
