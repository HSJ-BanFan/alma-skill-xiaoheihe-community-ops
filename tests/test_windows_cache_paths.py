import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from assets import owned
from xhh_sdk.exceptions import XhhConfigError


@unittest.skipUnless(os.name == "nt", "Windows device path equivalence")
class WindowsCachePathTests(unittest.TestCase):
    def test_device_prefix_and_normal_path_identify_the_same_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "cache"
            root.mkdir()
            target = root / "image.png"
            target.write_bytes(b"fixture")
            resolve = Path.resolve

            def alternate(path, *args, **kwargs):
                result = str(resolve(path, *args, **kwargs))
                result = result[4:] if result.startswith("\\\\?\\") else result
                return Path("\\\\?\\" + result) if path == root else Path(result)

            with patch.object(Path, "resolve", alternate):
                self.assertEqual(owned(root, target), target)

    def test_device_prefix_does_not_allow_a_different_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "cache"
            root.mkdir()
            outside = Path(temporary) / "outside.png"
            outside.write_bytes(b"fixture")
            resolve = Path.resolve

            def alternate(path, *args, **kwargs):
                result = str(resolve(path, *args, **kwargs))
                result = result[4:] if result.startswith("\\\\?\\") else result
                return Path("\\\\?\\" + result) if path == root else Path(result)

            with patch.object(Path, "resolve", alternate), self.assertRaises(XhhConfigError):
                owned(root, outside)


if __name__ == "__main__":
    unittest.main()
