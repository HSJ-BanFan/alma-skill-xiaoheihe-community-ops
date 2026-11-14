import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import xhh
from observations import Store, difference
from test_paging import PagedFixture
from xhh_sdk.exceptions import XhhConfigError


class ObservationTests(unittest.TestCase):
    def test_delta_does_not_infer_deletion_or_timing_changes(self):
        before = {"items": [{"id": "1", "title": "same", "source": {"observed_at": 1}}, {"id": "2", "title": "old"}]}
        after = {"items": [{"id": "1", "title": "same", "source": {"observed_at": 2}}, {"id": "3", "title": "new"}]}
        delta = difference(before, after)
        self.assertEqual(delta["unchanged"], ["1"])
        self.assertEqual(delta["not_observed"], ["2"])
        self.assertEqual(delta["new"], ["3"])
        self.assertFalse(delta["deletion_inferred"])

    def test_changed_record_and_uid_isolation(self):
        self.assertEqual(difference({"id": "1", "title": "old"}, {"id": "1", "title": "new"})["changed"], ["1"])
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary))
            self.assertEqual(store.list("101"), [])
            self.assertFalse(store.path.exists())
            store.save("101", "baseline", "query", {"id": "1"}, {}, ["post", "1"])
            self.assertEqual(store.get("101", "baseline")["data"]["id"], "1")
            with self.assertRaises(XhhConfigError):
                store.get("202", "baseline")

    def test_cli_persisted_resume_after_new_client(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            config = folder / "fixture.json"
            config.write_text(json.dumps({"pkey": "fixture", "heybox_id": "101", "protocol_mode": "web"}))
            def call(*args):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = xhh.main(["--config", str(config), "--data-dir", str(folder), *args], client_factory=lambda cfg: PagedFixture())
                return code, json.loads(output.getvalue())
            code, initial = call("comments", "123", "--snapshot", "read-1")
            self.assertEqual(code, 0)
            code, resumed = call("snapshot", "resume", "read-1")
            self.assertEqual(code, 0)
            self.assertEqual(resumed["data"]["items"][1]["id"], "11")
            code, saved = call("snapshot", "show", "read-1")
            self.assertEqual(code, 0)
            self.assertEqual(saved["data"]["coverage"]["position"], 2)


if __name__ == "__main__":
    unittest.main()
