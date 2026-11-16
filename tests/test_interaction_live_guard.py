from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request

ROOT = Path(os.environ.get("XHH_TEST_SKILL_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT / "scripts"), str(Path(__file__).resolve().parent)]

import verify_interaction_live


class LiveReadGuardTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def request(route, *args, **kwargs):
            self.calls.append((route, args, kwargs))
            return {"result": "fixture"}

        client = SimpleNamespace(transport=SimpleNamespace(signed_request=request, _open=request))
        with patch.object(verify_interaction_live, "XhhClient", return_value=client):
            self.client = verify_interaction_live.guarded_client(SimpleNamespace())

    def test_forum_writes_are_rejected_before_dispatch(self):
        for route in ("/bbs/app/comment/create", "/bbs/app/link/like/combo",
                      "/bbs/app/link/collect", "/bbs/app/api/qcloud/cos/upload/info/v2"):
            with self.assertRaisesRegex(RuntimeError, "non-read request"):
                self.client.transport.signed_request(route)
        self.assertEqual(self.calls, [])

    def test_payload_on_allowed_route_is_rejected_before_dispatch(self):
        with self.assertRaisesRegex(RuntimeError, "non-read request"):
            self.client.transport.signed_request("/account/restore_login", payload={"fixture": True})
        self.assertEqual(self.calls, [])

    def test_selected_read_route_preserves_query(self):
        result = self.client.transport.signed_request("/bbs/app/profile/bbs/comment/list", query={"limit": "2"})
        self.assertEqual(result, {"result": "fixture"})
        self.assertEqual(self.calls, [("/bbs/app/profile/bbs/comment/list", (), {"query": {"limit": "2"}})])

    def test_raw_upload_and_forum_requests_are_rejected_before_open(self):
        for request in (Request("https://api.xiaoheihe.cn/account/restore_login", data=b"fixture", method="POST"),
                        Request("https://cdn.example.com/upload", data=b"fixture", method="PUT"),
                        Request("https://api.xiaoheihe.cn/bbs/app/comment/create")):
            with self.assertRaisesRegex(RuntimeError, "non-read request"):
                self.client.transport._open(request)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
