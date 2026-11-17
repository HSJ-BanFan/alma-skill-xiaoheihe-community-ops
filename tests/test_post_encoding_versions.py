import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from messages import MessageError, RENDERER_VERSION, compose, validate_prepared


class PostEncodingVersionTests(unittest.TestCase):
    def test_previous_web_post_version_cannot_change_wire_encoding_on_send(self):
        with tempfile.TemporaryDirectory() as folder:
            data_dir = Path(folder) / "unused"
            prepared = compose({"kind": "post", "content": [{"type": "native_emoji", "id": "cube:32"}]},
                               data_dir, protocol="web")
            old = copy.deepcopy(prepared)
            old["renderer_version"] = RENDERER_VERSION
            identity = {key: old[key] for key in ("spec", "protocol", "catalog_version",
                        "catalog_fingerprint", "renderer_version", "image_hashes")}
            old["fingerprint"] = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")).encode("utf-8")).hexdigest()
            with self.assertRaises(MessageError) as changed:
                validate_prepared(old, data_dir, protocol="web")
            self.assertEqual(changed.exception.code, "prepared_message_changed")
            self.assertEqual(validate_prepared(prepared, data_dir, protocol="web")["fingerprint"],
                             prepared["fingerprint"])
            self.assertFalse(data_dir.exists())

    def test_app_posts_and_non_post_messages_keep_their_previous_version(self):
        with tempfile.TemporaryDirectory() as folder:
            data_dir = Path(folder) / "unused"
            specs = [({"kind": "post", "content": [{"type": "text", "text": "body"}]}, "app"),
                     ({"kind": "comment", "target": {"link_id": "501"},
                       "content": [{"type": "text", "text": "body"}]}, "web")]
            for spec, protocol in specs:
                prepared = compose(spec, data_dir, protocol=protocol)
                self.assertEqual(prepared["renderer_version"], RENDERER_VERSION)
            self.assertFalse(data_dir.exists())


if __name__ == "__main__":
    unittest.main()
