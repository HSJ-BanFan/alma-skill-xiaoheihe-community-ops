import contextlib
import ctypes as C
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "scripts"), str(Path(__file__).resolve().parent)]
import xhh
from documents import document
from network_io import AuthenticatedRedirects, read_http
from observations import Store, SnapshotConflict
from test_paging import PagedFixture
from xhh_sdk.exceptions import XhhConfigError


class CacheRecoveryTests(unittest.TestCase):
    def run_cli(self, root, arguments, *, client=None):
        config = root / "fixture.json"
        config.write_text(json.dumps({"pkey": "fixture", "heybox_id": "101", "protocol_mode": "web"}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = xhh.main(["--config", str(config), "--data-dir", str(root), *arguments], client_factory=lambda cfg: client or PagedFixture())
        return code, json.loads(output.getvalue())

    def test_cross_origin_and_downgrade_redirects_do_not_forward_cookie(self):
        request = Request("https://api.xiaoheihe.cn/read", headers={"Cookie": "fixture-secret"})
        policy = AuthenticatedRedirects(request.full_url)
        for target in ("https://example.com/read", "http://api.xiaoheihe.cn/read", "https://api.xiaoheihe.cn:8443/read"):
            with self.assertRaises(XhhConfigError):
                policy.redirect_request(request, None, 302, "redirect", {}, target)

    def test_network_command_cannot_be_selected_by_caller(self):
        with patch("network_io.subprocess.run") as process, self.assertRaises(TypeError):
            read_http(Request("https://api.xiaoheihe.cn/read"), 1, _command=["anything"])
        process.assert_not_called()

    def test_stale_snapshot_writer_cannot_replace_newer_observations(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary))
            first = store.save("101", "data", "query", {"id": "1"}, {}, [])
            second = store.save("101", "data", "query", {"id": "2"}, {}, [], expected_revision=first)
            with self.assertRaises(SnapshotConflict):
                store.save("101", "data", "query", {"id": "3"}, {}, [], expected_revision=first)
            self.assertEqual(store.get("101", "data")["data"]["id"], "2")
            self.assertEqual(store.get("101", "data")["revision"], second)

    def test_saved_response_is_replayed_after_stdout_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "fixture.json"
            config.write_text(json.dumps({"pkey": "fixture", "heybox_id": "101", "protocol_mode": "web"}))
            with patch("xhh.emit", side_effect=BrokenPipeError):
                code = xhh.main(["--config", str(config), "--data-dir", str(root), "comments", "123", "--snapshot", "read"], client_factory=lambda cfg: PagedFixture())
            self.assertEqual(code, 1)
            self.assertIsNotNone(Store(root).get("101", "read")["pending_response"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = xhh.main(["--config", str(config), "--data-dir", str(root), "snapshot", "resume", "read"],
                                client_factory=lambda cfg: (_ for _ in ()).throw(AssertionError("replay must be offline")))
            self.assertEqual(code, 0)
            value = json.loads(output.getvalue())
            self.assertTrue(value["coverage"]["replayed_delivery"])
            self.assertEqual(value["data"]["items"][1]["id"], "10")
            self.assertIsNone(Store(root).get("101", "read")["pending_response"])

    def test_manual_resume_preserves_existing_named_pages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code, first = self.run_cli(root, ["comments", "123", "--snapshot", "read"])
            self.assertEqual(code, 0)
            code, second = self.run_cli(root, ["comments", "123", "--resume", first["coverage"]["resume"], "--snapshot", "read"])
            self.assertEqual(code, 0)
            saved = Store(root).get("101", "read")
            self.assertEqual([item["id"] for item in saved["data"]["items"]], ["9", "10", "11"])
            self.assertEqual(saved["coverage"]["observed_positions"], [1, 2])
            self.assertEqual(saved["coverage"]["duplicates_removed_since_snapshot"], 1)

    def test_named_thread_resumes_with_prior_ancestry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(self.run_cli(root, ["thread", "123", "9", "--around", "11", "--snapshot", "thread"])[0], 0)
            code, value = self.run_cli(root, ["snapshot", "resume", "thread"])
            self.assertEqual(code, 0)
            self.assertEqual(value["data"]["ancestor_ids"], ["11", "10", "9"])
            self.assertTrue(value["data"]["ancestry_complete"])

    def test_source_distinguishes_configured_namespace_from_verified_identity(self):
        class WrongIdentity(PagedFixture):
            def account_info(self):
                return {"userid": "999"}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code, ordinary = self.run_cli(root, ["comments", "123"], client=WrongIdentity())
            self.assertEqual(code, 0)
            self.assertEqual(ordinary["data"]["source"]["configured_uid"], "101")
            self.assertFalse(ordinary["data"]["source"]["identity_verified"])
            self.assertNotIn("account_uid", ordinary["data"]["source"])

            code, verified = self.run_cli(root, ["comments", "123", "--snapshot", "verified"])
            self.assertEqual(code, 0)
            self.assertEqual(verified["data"]["source"]["configured_uid"], "101")
            self.assertTrue(verified["data"]["source"]["identity_verified"])

    def test_identity_mismatch_prevents_snapshot_creation(self):
        class WrongIdentity(PagedFixture):
            def account_info(self):
                return {"userid": "999"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code, value = self.run_cli(root, ["comments", "123", "--snapshot", "bad"], client=WrongIdentity())
            self.assertEqual(code, 1)
            self.assertEqual(value["error"]["code"], "account_identity_not_verified")
            self.assertFalse(Store(root).path.exists())

    def test_bracketed_plaintext_and_inline_whitespace_survive(self):
        for text in ("[求助] how", "[cube_笑cry] hello", "[1,2]", '[{"example":1}]'):
            self.assertEqual(document({"id": 1, "text": text}, {})["body"]["text"], text)
        body = document({"id": 1, "text": '<p>hello <a href="https://example.com">world</a> again</p>'}, {})["body"]
        self.assertEqual(body["text"], "hello world again")
        self.assertEqual(document({"id": 1, "text": ""}, {})["body"]["completeness"], "unknown")

    def test_private_snapshot_storage_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary))
            store.save("101", "private", "query", {"id": "1"}, {}, [])
            if sys.platform != "win32":
                self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(store.path.parent.stat().st_mode & 0o777, 0o700)
            else:
                from ctypes import wintypes as W
                security, kernel = C.WinDLL("advapi32"), C.WinDLL("kernel32")
                security.GetNamedSecurityInfoW.argtypes = [W.LPCWSTR, C.c_int, W.DWORD, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p)]
                security.GetNamedSecurityInfoW.restype = W.DWORD
                security.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p]
                kernel.LocalFree.argtypes = [C.c_void_p]
                descriptor, text = C.c_void_p(), C.c_void_p()
                try:
                    self.assertEqual(security.GetNamedSecurityInfoW(str(store.path.parent), 1, 4, None, None, None, None, C.byref(descriptor)), 0)
                    self.assertTrue(security.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4, C.byref(text), None))
                    sddl = C.wstring_at(text)
                    self.assertIn("D:P", sddl)
                    for trustee in ("WD", "BU", "AU"):
                        self.assertNotIn(";;;" + trustee + ")", sddl)
                finally:
                    if text:
                        kernel.LocalFree(text)
                    if descriptor:
                        kernel.LocalFree(descriptor)


if __name__ == "__main__":
    unittest.main()
