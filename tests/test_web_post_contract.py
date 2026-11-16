"""Synthetic wire and CLI checks. No live publishing or visibility evidence."""
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import xhh
from messages import compose
from operations import Journal
from xhh_sdk.client import XhhClient
from xhh_sdk.config import XhhConfig
from xhh_sdk.exceptions import XhhAPIError, XhhConfigError
from xhh_sdk.payload import Post
from xhh_sdk.transport import Transport

POST_ROUTE = "/bbs/app/api/link/post"
QUAD = {"_time": "1700000000", "nonce": "synthetic-nonce", "hkey": "synthetic-hkey",
        "_rnd": "15:synthetic-existing-rnd", "version": "999.0.4"}
WEB_QUERY = {"os_type": "web", "app": "heybox", "x_client_type": "weboutapp",
             "x_client_version": "", "x_os_type": "Windows", "x_app": "heybox",
             "heybox_id": "101", "_time": QUAD["_time"], "nonce": QUAD["nonce"],
             "hkey": QUAD["hkey"], "version": QUAD["version"]}
PRIVATE = "SYNTHETIC_PRIVATE_UPSTREAM_9a8b"
BODY = "SYNTHETIC_PRIVATE_BODY_c7d6"


class Response:
    def __init__(self, value):
        self.body = json.dumps(value, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=None):
        return self.body


class WebPostContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xhh-synthetic-post-")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.config = XhhConfig(pkey="synthetic-session", heybox_id="101", protocol_mode="web")
        self.requests = []
        self.response = {"status": "ok", "result": {"link_id": "801"}}
        open_patch = patch("urllib.request.urlopen", side_effect=self.respond)
        self.addCleanup(open_patch.stop)
        open_patch.start()
        sign_patch = patch("xhh_sdk.transport.sign_web_request", return_value=QUAD)
        self.addCleanup(sign_patch.stop)
        self.sign = sign_patch.start()

    def respond(self, request, timeout=None):
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return Response(self.response)

    def assert_wire(self, request, path, method, query, payload=None):
        self.assertEqual(urlsplit(request.full_url).path, path)
        self.assertEqual(request.get_method(), method)
        self.assertEqual(parse_qs(urlsplit(request.full_url).query, keep_blank_values=True),
                         {key: [str(value)] for key, value in query.items()})
        self.assertEqual(request.data, urlencode(payload).encode("utf-8") if payload is not None else None)
        self.assertEqual(request.get_header("Cookie"), self.config.cookie_web)
        self.assertEqual(request.get_header("Origin"), self.config.web_origin)
        self.assertEqual(request.get_header("Referer"), self.config.web_origin + "/")
        self.assertEqual(request.get_header("User-agent"), self.config.user_agent_web)
        self.assertEqual(request.get_header("Content-type"),
                         "application/x-www-form-urlencoded" if method == "POST" else None)

    def cli(self, data, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                patch("xhh.load_config", return_value=self.config), \
                patch.object(XhhClient, "account_info", return_value={"heybox_id": "101"}):
            code = xhh.main(["--data-dir", str(data), *args])
        self.assertEqual(errors.getvalue(), "")
        return code, json.loads(output.getvalue()), output.getvalue()

    def send(self, data, prepared):
        return self.cli(data, "send", "--prepared-json", json.dumps(prepared, ensure_ascii=False),
                        "--operation-id", "synthetic-boundary", "--confirm")

    def assert_closed_metadata(self, data, forbidden):
        operation = Journal(data).read("101", "synthetic-boundary")
        self.assertEqual(operation["status"], "uncertain")
        self.assertIsNone(operation["receipt"])
        self.assertNotIn("diagnostic", operation)
        self.assertEqual(operation["context"]["expected"]["emoji_ids"], ["cube:32"])
        with contextlib.closing(sqlite3.connect(data / "operations.sqlite3")) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual([row[1] for row in connection.execute("PRAGMA table_info(operations)")],
                             ["uid", "operation_id", "fingerprint", "kind", "status", "receipt", "updated_at"])
            self.assertEqual(connection.execute("SELECT status,receipt FROM operations").fetchall(),
                             [("uncertain", None)])
            dump = "\n".join(connection.iterdump())
        for sentinel in [BODY, PRIVATE, "diagnostic", *forbidden]:
            self.assertNotIn(sentinel, dump)

    def test_web_post_sends_existing_rnd_with_unchanged_create_and_edit_payload_bytes(self):
        client = XhhClient(self.config)
        for edit_id in (None, "801"):
            with self.subTest(edit_id=edit_id):
                post = Post(content="<p>native <span data-emoji=\"cube_笑cry\"></span></p>",
                            images=["https://example.invalid/synthetic-sticker.png"], edit_link_id=edit_id)
                payload = post.build()
                self.assertEqual(payload["link_tag"], "1")
                self.assertEqual(payload.get("edit"), "1" if edit_id else None)
                client.publish(post, upload_local=False)
                query = parse_qs(urlsplit(self.requests[-1].full_url).query)
                self.assertEqual(query.get("_rnd"), [QUAD["_rnd"]])
                self.assert_wire(self.requests[-1], POST_ROUTE, "POST", {**WEB_QUERY, "_rnd": QUAD["_rnd"]}, payload)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.sign.call_count, 2)

    def test_normalized_post_route_includes_rnd_for_explicit_post(self):
        Transport(self.config).web_signed_request(POST_ROUTE + "/", method="POST")
        self.assert_wire(self.requests[-1], POST_ROUTE + "/", "POST", {**WEB_QUERY, "_rnd": QUAD["_rnd"]})

    def test_recognized_failure_preserves_sdk_status(self):
        for message in ("缺失参数", "发布功能暂不可用"):
            with self.subTest(message=message):
                self.response = {"status": "failed", "msg": message}
                with self.assertRaises(XhhAPIError) as caught:
                    Transport(self.config).web_signed_request(POST_ROUTE, payload={"draft": "1"})
                self.assertEqual(caught.exception.status, "failed")

    def test_recognized_failed_posts_report_fixed_cli_diagnostics_without_resending_or_persisting_text(self):
        for index, (message, token) in enumerate([
                ("缺失参数", "web_post_missing_parameter"),
                ("发布功能暂不可用", "web_post_publishing_unavailable")]):
            with self.subTest(message=message):
                data = self.folder / str(index)
                prepared = compose({"kind": "post", "content": [{"type": "text", "text": BODY},
                    {"type": "native_emoji", "id": "cube:32"}]}, data)
                self.response = {"status": "failed", "msg": message, "private": PRIVATE}
                before = len(self.requests)
                code, value, output = self.send(data, prepared)
                self.assertEqual(code, 1)
                self.assertFalse(value["ok"])
                self.assertEqual(value["error"], {"code": "write_uncertain", "retry_allowed": False,
                    "diagnostic": {"code": token, "path": POST_ROUTE, "status": "failed", "message": message}})
                self.assertEqual(value["data"]["operation"]["status"], "uncertain")
                self.assertIsNone(value["data"]["receipt"])
                for sentinel in (PRIVATE, BODY, "Traceback", "platform returned"):
                    self.assertNotIn(sentinel, output)
                self.assert_closed_metadata(data, [message, token])
                code, second, _ = self.send(data, prepared)
                self.assertEqual(code, 1)
                self.assertEqual(second["error"], {"code": "operation_pending_or_uncertain", "retry_allowed": False})
                self.assertEqual(len(self.requests) - before, 1)
                code, lookup, output = self.cli(data, "operation", "synthetic-boundary")
                self.assertEqual(code, 0)
                self.assertEqual(lookup["data"]["status"], "uncertain")
                self.assertNotIn("diagnostic", output)

    def test_unknown_or_arbitrary_failures_never_project_private_messages(self):
        forged = XhhAPIError(PRIVATE)
        forged.diagnostic = {"message": PRIVATE}
        failures = [{"status": "failed", "msg": PRIVATE, "body": BODY},
                    {"status": "other-" + PRIVATE, "msg": "缺失参数"},
                    {"status": "failed", "msg": "缺失参数 " + PRIVATE},
                    {"status": "failed", "msg": {"private": PRIVATE}},
                    {"status": "failed", "msg": [PRIVATE]}, RuntimeError(PRIVATE + BODY), forged]
        for index, failure in enumerate(failures):
            with self.subTest(index=index):
                data = self.folder / str(index)
                prepared = compose({"kind": "post", "content": [{"type": "text", "text": BODY},
                    {"type": "native_emoji", "id": "cube:32"}]}, data)
                self.response = failure
                before = len(self.requests)
                code, value, output = self.send(data, prepared)
                self.assertEqual(code, 1)
                self.assertEqual(value["error"], {"code": "write_uncertain", "retry_allowed": False})
                self.assertIsNone(value["data"]["receipt"])
                for sentinel in (PRIVATE, BODY, "Traceback", "diagnostic", "platform returned"):
                    self.assertNotIn(sentinel, output)
                self.assert_closed_metadata(data, [])
                self.assertEqual(self.send(data, prepared)[1]["error"]["code"], "operation_pending_or_uncertain")
                self.assertEqual(len(self.requests) - before, 1)

    def test_web_get_and_other_posts_keep_existing_wire_and_diagnostic_behavior(self):
        transport = Transport(self.config)
        for path, payload in [("/bbs/app/profile/post/limits", None), (POST_ROUTE, None),
                              (POST_ROUTE + "/", None),
                              ("/bbs/app/api/qcloud/cos/upload/info/v2", {"files": "[]"}),
                              ("/bbs/app/api/qcloud/cos/upload/token/v2", {"key": "synthetic"}),
                              ("/bbs/app/api/qcloud/cos/upload/callback/v2", {"key": "synthetic"}),
                              ("/bbs/app/comment/create", {"text": BODY, "imgs": ""})]:
            with self.subTest(path=path, payload=payload):
                method = "GET" if payload is None else "POST"
                upload = "/qcloud/cos/upload/" in path
                if upload:
                    transport.web_request(path, payload)
                else:
                    transport.signed_request(path, query={"h_src": ""}, payload=payload)
                query = WEB_QUERY if upload else {**WEB_QUERY, "h_src": ""}
                self.assert_wire(self.requests[-1], path, method, query, payload)
                self.response = {"status": "failed", "msg": "缺失参数"}
                with self.assertRaises(XhhAPIError) as caught:
                    transport.signed_request(path, payload=payload)
                self.assertIsNone(getattr(caught.exception, "diagnostic", None))
                self.response = {"status": "ok", "result": {"link_id": "801"}}

    def test_app_wire_and_nested_only_receipts_are_unchanged(self):
        config = XhhConfig(pkey="synthetic-session", heybox_id="101", protocol_mode="app")
        signer = SimpleNamespace(sign=lambda path: {"path": path, **QUAD})
        transport = Transport(config, signer=signer)
        payload = {"text": BODY, "draft": "1"}
        for body in (None, payload):
            transport.signed_request(POST_ROUTE, query={"h_src": ""}, payload=body)
            request = self.requests[-1]
            expected = {"os_type": "Android", "x_os_type": "Android", "x_client_type": "mobile",
                "os_version": "14", "version": QUAD["version"], "build": "1093", "channel": "heybox_google",
                "x_app": "heybox", "time_zone": "Asia/Shanghai", "dw": "360", "netmode": "wifi",
                "imei": config.imei, "device_info": config.device_info, "heybox_id": "101", **QUAD, "h_src": ""}
            self.assertEqual(parse_qs(urlsplit(request.full_url).query, keep_blank_values=True),
                             {key: [value] for key, value in expected.items()})
            self.assertEqual(request.data, urlencode(body).encode("utf-8") if body else None)
            self.assertEqual(request.get_header("Cookie"), config.cookie_app)
        self.response = {"status": "failed", "msg": "缺失参数"}
        with self.assertRaises(XhhAPIError) as caught:
            transport.signed_request(POST_ROUTE, payload=payload)
        self.assertIsNone(getattr(caught.exception, "diagnostic", None))
        self.response = {"status": "ok", "result": {}}
        upload_path = "/bbs/app/api/qcloud/cos/upload/info/v2"
        transport.web_request(upload_path, payload)
        request = self.requests[-1]
        self.assertEqual(request.full_url, config.api_base + upload_path)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, urlencode(payload).encode("utf-8"))
        self.assertEqual(request.get_header("Cookie"), config.cookie_web)
        client = XhhClient(config)
        client.transport = transport
        self.response = {"status": "ok", "link_id": False, "result": {"link_id": "801"}}
        self.assertEqual(client.publish(Post(content="synthetic"), upload_local=False)["link_id"], "801")
        for response in ({"status": "ok", "link_id": "801", "result": {}},
                         {"status": "ok", "link_id": "801"},
                         {"status": "ok", "result": {"link_id": True}}):
            self.response = response
            with self.assertRaises(XhhAPIError):
                client.publish(Post(content="synthetic"), upload_local=False)

    def test_protected_query_overrides_fail_before_dispatch_or_signing(self):
        for mode in ("web", "app"):
            config = XhhConfig(pkey="synthetic-session", heybox_id="101", protocol_mode=mode)
            transport = Transport(config)
            for key in ("_rnd", "_time", "nonce", "hkey", "heybox_id", "pkey", "x_pkey", "imei", "device_info"):
                with self.subTest(mode=mode, key=key), self.assertRaises(XhhConfigError):
                    transport.signed_request(POST_ROUTE, query={key: "synthetic-override"}, payload={"text": BODY})
        self.assertEqual(self.requests, [])
        self.sign.assert_not_called()

    def test_web_receipts_accept_top_or_nested_and_reject_invalid_or_conflicting_ids(self):
        client = XhhClient(self.config)
        post = Post(content="synthetic")
        for response in ({"status": "ok", "link_id": 801, "result": {}},
                         {"status": "success", "link_id": "801"},
                         {"status": "ok", "result": {"link_id": 801}},
                         {"status": "ok", "link_id": "801", "result": {"link_id": 801}},
                         {"status": "ok", "link_id": None, "result": {"link_id": "801"}},
                         {"status": "ok", "link_id": "801", "result": {"link_id": None}}):
            with self.subTest(response=response):
                self.response = response
                receipt = client.publish(post, upload_local=False)
                self.assertEqual(receipt["link_id"], "801")
                self.assertEqual(receipt["url"], "https://www.xiaoheihe.cn/app/bbs/link/801")
                self.assertTrue(receipt["draft"])
        invalid = [True, False, 0, -1, 1.5, "", "01", "801x", "9" * 31, [], {}]
        responses = [{"status": "ok", "result": {}},
                     {"status": "ok", "link_id": None, "result": {}},
                     {"status": "ok", "result": {"link_id": None}},
                     {"status": "ok", "link_id": None, "result": {"link_id": None}},
                     {"status": "ok", "link_id": "801", "result": {"link_id": "802"}}]
        for value in invalid:
            responses.extend([{"status": "ok", "link_id": value, "result": {}},
                              {"status": "ok", "link_id": value, "result": {"link_id": "801"}},
                              {"status": "ok", "link_id": "801", "result": {"link_id": value}}])
        for response in responses:
            with self.subTest(response=response):
                self.response = response
                with self.assertRaises(XhhAPIError):
                    client.publish(post, upload_local=False)


if __name__ == "__main__":
    unittest.main()
