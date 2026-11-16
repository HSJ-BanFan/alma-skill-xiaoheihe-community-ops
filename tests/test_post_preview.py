from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(os.environ.get("XHH_TEST_SKILL_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "scripts"))

import xhh
from operations import Journal
from xhh_sdk.exceptions import SDKDiagnostic, XhhAPIError

try:
    import markdown_it  # noqa: F401
    MARKDOWN_INSTALLED = True
except ImportError:
    MARKDOWN_INSTALLED = False

requires_markdown = unittest.skipUnless(MARKDOWN_INSTALLED, "markdown-it-py is not installed")

MARKDOWN_SPEC = {
    "title": "标题",
    "content": "# 标题\n\n**粗体** 与 [链接](https://example.com)\n\n"
               "![图](https://img.example/a.png)\n\n> 引用\n\n```\n  x = 1\n    y = 2\n```",
    "post_format": "article", "content_format": "markdown",
    "topic_ids": ["7214"],
    "original": False,
}


class Response:
    def __init__(self, value):
        self.body = json.dumps(value, ensure_ascii=False).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, *args):
        return self.body


class PostFixture:
    def __init__(self):
        self.calls = []
        self.requests = []
        self.send_error = None

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        path = urlsplit(request.full_url).path
        data = parse_qs(request.data.decode(), keep_blank_values=True) if request.data else {}
        self.calls.append((path, request.get_method(), data))
        if path == "/account/restore_login":
            result = {"userid": "101"}
        elif path == "/bbs/app/api/link/post":
            if self.send_error is not None:
                raise self.send_error
            result = {"link_id": "801"}
        else:
            result = {}
        return Response({"status": "ok", "result": result})

    def posts(self):
        return [call for call in self.calls if call[0] == "/bbs/app/api/link/post"]

    def wire(self):
        return {key: values[0] for key, values in self.posts()[-1][2].items()}


class PostPreviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.data = self.folder / "data"
        self.config = self.folder / "fixture.json"
        self.config.write_text(json.dumps({"pkey": "fixture-secret-only", "heybox_id": "101",
                                           "protocol_mode": "web"}))
        self.fixture = PostFixture()

    def tearDown(self):
        self.temp.cleanup()

    def write_spec(self, spec, name="post.json"):
        path = self.folder / name
        path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        return path

    def run_cli(self, *arguments):
        output = io.StringIO()
        with patch("urllib.request.urlopen", self.fixture), \
             patch("paging.read_http", side_effect=lambda request, timeout, **kwargs: self.fixture(request, timeout)), \
             contextlib.redirect_stdout(output):
            code = xhh.main(["--config", str(self.config), "--data-dir", str(self.data), *arguments])
        return code, json.loads(output.getvalue())

    def preview(self, spec, *extra):
        return self.run_cli("preview-post", str(self.write_spec(spec)), *extra)

    def publish(self, spec, operation_id, *extra):
        return self.run_cli("publish", str(self.write_spec(spec)), "--operation-id", operation_id,
                            "--confirm", *extra)

    def test_preview_uses_no_account_network_operation_or_state(self):
        self.assertNotIn("preview-post", xhh.WRITES)
        spec = self.write_spec({"content": "正文", "content_format": "text"})
        before = spec.read_bytes()
        output = io.StringIO()
        with patch("urllib.request.urlopen", side_effect=AssertionError("network used")), \
             contextlib.redirect_stdout(output):
            code = xhh.main(["--data-dir", str(self.data), "preview-post", str(spec)])
        value = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(set(value["data"]), {"spec", "payload", "media"})
        self.assertEqual((value["coverage"]["network"], value["coverage"]["account_checked"],
                          value["coverage"]["operation_id_reserved"]),
                         ("not_used", False, False))
        self.assertFalse(self.data.exists())
        self.assertEqual(spec.read_bytes(), before)
        self.assertEqual(self.fixture.calls, [])

    @requires_markdown
    def test_matching_preview_and_publish_send_the_same_payload(self):
        spec = self.write_spec(MARKDOWN_SPEC)
        before = spec.read_bytes()
        code, preview = self.preview(MARKDOWN_SPEC)
        self.assertEqual(code, 0)
        self.assertEqual(spec.read_bytes(), before)
        code, published = self.publish(MARKDOWN_SPEC, "markdown-1")
        self.assertEqual(code, 0)
        self.assertEqual(preview["data"]["payload"], self.fixture.wire())
        self.assertEqual(published["data"]["receipt"]["link_id"], "801")
        self.assertEqual(self.fixture.calls[0][0], "/account/restore_login")
        self.assertEqual(len(self.fixture.posts()), 1)
        self.assertEqual(preview["coverage"]["markdown_renderer"]["version"], "4.2.0")

    def test_article_cover_and_visibility_reach_the_wire(self):
        spec = {**MARKDOWN_SPEC, "content": "<p>Article body</p>", "post_format": "article", "content_format": "html",
                "post_type": "3", "visibility": "2",
                "cover_url": "https://imgheybox.max-c.com/cover.png"}
        code, preview = self.preview(spec)
        self.assertEqual(code, 0)
        code, _ = self.publish(spec, "article-1")
        self.assertEqual(code, 0)
        wire = self.fixture.wire()
        self.assertEqual((wire["post_type"], wire["view_limit"], wire["thumb"]),
                         ("1", "2", "https://imgheybox.max-c.com/cover.png"))
        self.assertEqual(wire["link_tag"], "11")
        self.assertEqual(preview["data"]["payload"], wire)

    def test_preview_public_flag_changes_only_the_draft_field(self):
        spec = {"content": "正文", "content_format": "text"}
        _, draft = self.preview(spec)
        _, public = self.preview(spec, "--public")
        self.assertEqual((draft["data"]["payload"]["draft"], public["data"]["payload"]["draft"]), ("1", "0"))
        self.assertEqual((draft["data"]["spec"]["draft"], public["data"]["spec"]["draft"]), (True, False))

    def test_app_preview_uses_explicit_legacy_protocol_without_account_access(self):
        spec = self.write_spec({"content": "body", "post_format": "article", "visibility": "0"})
        code, preview = self.run_cli("--protocol", "app", "preview-post", str(spec))
        self.assertEqual(code, 0)
        self.assertEqual(preview["coverage"]["protocol"], "app")
        self.assertEqual((preview["data"]["payload"]["post_type"], preview["data"]["payload"]["link_tag"],
                          preview["data"]["payload"]["view_limit"]), ("3", "1", "0"))
        self.assertEqual(self.fixture.calls, [])
        self.assertFalse(self.data.exists())

    def test_web_form_matches_creator_encoding_and_session_cookie_names(self):
        code, result = self.publish({"content": "A B + 中文", "content_format": "text"}, "creator-form")
        self.assertEqual(code, 0, result)
        request = self.fixture.requests[-1]
        query = parse_qs(urlsplit(request.full_url).query, keep_blank_values=True)
        self.assertEqual(request.get_header("Content-type"), "application/x-www-form-urlencoded;charset=UTF-8")
        self.assertIn(b"A%20B%20%2B%20", request.data)
        self.assertNotIn(b"+", request.data)
        self.assertEqual(query["app"], ["heybox"])
        self.assertEqual(query["os_type"], ["web"])
        self.assertEqual(query["x_app"], ["heybox"])
        self.assertEqual(query["x_client_type"], ["weboutapp"])
        self.assertEqual(query["x_client_version"], [""])
        self.assertEqual(query["x_os_type"], ["Windows"])
        self.assertNotIn("web_version", query)
        self.assertNotIn("device_id", query)
        cookies = dict(pair.strip().split("=", 1) for pair in request.get_header("Cookie").split(";") if pair.strip())
        self.assertEqual(set(cookies), {"user_pkey", "user_heybox_id"})
        self.assertEqual(cookies["user_pkey"], "fixture-secret-only")
        self.assertEqual(cookies["user_heybox_id"], "101")

    def test_refusals_consume_no_operation_id_and_send_nothing(self):
        cases = {
            "format": {"content": "x", "content_format": "md"},
            "type": {"content": "x", "post_type": 1},
            "relative": {"content": "x", "images": ["relative.png"]},
            "http": {"content": "x", "images": ["http://example.com/a.png"]},
            "data": {"content": "x", "images": ["data:image/png;base64,aaaa"]},
            "bad-cover": {"content": "x", "post_type": "3", "cover_url": "http://example.com/a.png"},
            "invalid-visibility": {"content": "x", "post_type": "1", "visibility": "0"},
            "rich-image-text": {"content": "**x**", "post_format": "image_text", "content_format": "markdown"},
            "original-string": {"content": "x", "original": "false"},
        }
        for name, spec in cases.items():
            with self.subTest(case=name):
                operation_id = "refused-" + name
                code, value = self.publish(spec, operation_id)
                self.assertEqual(code, 1)
                self.assertEqual(value["ok"], False)
                code, value = self.preview(spec)
                self.assertEqual(code, 1)
                self.assertEqual(self.fixture.posts(), [])
                self.assertIsNone(Journal(self.data).read("101", operation_id))

    def test_sdk_rejection_is_explained_without_reopening_the_operation(self):
        self.fixture.send_error = XhhAPIError(
            "Web post failed",
            status="failed",
            diagnostic=SDKDiagnostic.WEB_POST_MISSING_PARAMETER,
        )
        spec = {"content": "body", "content_format": "text", "original": False}
        code, result = self.publish(spec, "platform-rejection")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "write_uncertain")
        self.assertEqual(result["error"]["diagnostic"], {
            "code": "web_post_missing_parameter",
            "path": "/bbs/app/api/link/post",
            "status": "failed",
            "message": "缺失参数",
        })
        self.assertNotIn("public_error", result["data"]["operation"])
        self.assertEqual(Journal(self.data).read("101", "platform-rejection")["status"], "uncertain")
        self.publish(spec, "platform-rejection")
        self.assertEqual(len(self.fixture.posts()), 1)

    def test_missing_markdown_dependency_names_the_requirements_file_without_state(self):
        spec = self.write_spec({"content": "# h", "post_format": "article", "content_format": "markdown"})
        with patch.dict(sys.modules, {"markdown_it": None}):
            code, value = self.preview({"content": "# h", "post_format": "article", "content_format": "markdown"})
            self.assertEqual(code, 1)
            self.assertIn("requirements-formatting.txt", value["error"]["message"])
            code, value = self.publish({"content": "# h", "post_format": "article", "content_format": "markdown"}, "no-markdown")
            self.assertEqual(code, 1)
            self.assertIn("requirements-formatting.txt", value["error"]["message"])
        self.assertEqual(self.fixture.posts(), [])
        self.assertIsNone(Journal(self.data).read("101", "no-markdown"))
        self.assertFalse(self.data.exists())
        self.assertTrue(spec.is_file())

    def test_text_and_html_preview_work_without_markdown_installed(self):
        with patch.dict(sys.modules, {"markdown_it": None}):
            for spec in ({"content": "正文", "content_format": "text"},
                         {"content": "<p>正文</p>", "post_format": "article", "content_format": "html"}):
                with self.subTest(spec=spec):
                    code, value = self.preview(spec)
                    self.assertEqual(code, 0)
                    block = json.loads(value["data"]["payload"]["text"])[0]
                    self.assertEqual(block, {"type": "html", "text": "<p>正文</p>"}
                                     if spec["content_format"] == "html" else {"type": "text", "text": "正文"})


if __name__ == "__main__":
    unittest.main()
