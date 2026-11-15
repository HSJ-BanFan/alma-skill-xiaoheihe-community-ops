from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(os.environ.get("XHH_TEST_SKILL_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "scripts"))

from interaction_history import InteractionHistory, Scope
from operations import Journal


def comment(cid, author, *, parent=None, root="601", text="text", created=10, updated=None):
    return {"id": cid, "post_id": "501", "root_id": root, "author": {"uid": author},
            "reply_to_id": parent, "text": text, "created_at": created, "updated_at": updated}


class InteractionSemanticsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xhh-semantics-")
        self.folder = Path(self.temp.name).resolve()
        self.assertTrue(self.folder.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.store = InteractionHistory(self.folder / "state")

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, nodes, kind="public", at=100):
        self.store.capture("101", nodes, kind, {"route": "fixture", "observed_at": at,
                                               "identity_verified_this_read": True})

    def test_peer_history_keeps_my_initial_message_that_received_the_reply(self):
        self.capture([comment("601", "101", text="my initial statement"),
                      comment("602", "202", parent="601", text="peer responds", created=20)])
        scope = Scope("101", "501", "202", "601")
        state = self.store.status(scope)
        self.assertEqual(state["participation"], "confirmed")
        self.assertEqual(state["my_known_comment_count"], 1)
        self.assertEqual(state["completed_round_count"], 0)
        history = self.store.history(scope, limit=20)
        self.assertEqual({row["id"] for row in history["items"]}, {"601", "602"})

    def test_ancestor_context_does_not_count_another_peers_round(self):
        self.capture([comment("609", "303", root="609", text="other peer starts"),
                      comment("610", "101", root="609", parent="609", text="my earlier response", created=20),
                      comment("612", "202", root="609", parent="610", text="requested peer enters", created=30),
                      comment("613", "101", root="609", parent="612", text="my response to requested peer", created=40)])
        scope = Scope("101", "501", "202", "609")
        self.assertEqual(self.store.status(scope)["completed_round_count"], 1)
        history = self.store.history(scope, limit=20)
        self.assertEqual({row["id"] for row in history["items"]}, {"609", "610", "612", "613"})

    def test_recent_history_uses_message_time_not_refresh_time(self):
        older = comment("601", "202", text="older", created=10)
        newer = comment("602", "101", parent="601", text="newer", created=20)
        self.capture([older, newer], at=100)
        self.capture([older], at=200)
        first = self.store.history(Scope("101", "501"), limit=1)
        self.assertEqual(first["items"][0]["id"], "602")
        self.assertEqual(first["items"][0]["created_at"], 20)

    def test_later_platform_edit_overrides_old_public_text_but_not_with_receipt(self):
        journal = Journal(self.folder / "state")
        payload = {"link_id": "501", "root_id": "601", "reply_id": "601", "text": "stale submitted body"}
        with patch("operations.project_receipts", side_effect=OSError("fixture projection interruption")):
            returned = journal.execute("101", "late-receipt", "reply", payload, lambda: {"comment_id": "701"}, identity_verified=True)
        self.assertEqual(returned["status"], "returned")
        self.capture([comment("701", "101", parent="601", text="public original", updated=10)], at=100)
        self.capture([comment("701", "101", parent="601", text="new own-list edit", updated=20)], kind="own_list", at=200)
        first = self.store.history(Scope("101", "501"))["items"][0]
        self.assertEqual(first["text"], "new own-list edit")
        self.assertEqual(first["public_seen_at"], 100)
        self.assertEqual(first["own_list_seen_at"], 200)
        replay = journal.execute("101", "late-receipt", "reply", payload, lambda: self.fail("duplicate POST"), identity_verified=True)
        self.assertTrue(replay["replayed"])
        current = self.store.history(Scope("101", "501"))["items"][0]
        self.assertEqual(current["text"], "new own-list edit")


if __name__ == "__main__":
    unittest.main()
