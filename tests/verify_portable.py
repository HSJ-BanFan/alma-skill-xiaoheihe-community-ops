"""Run a text-only skill copy outside its checkout with isolated Python."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    snapshot = json.loads((ROOT / "scripts/xhh_sdk/SNAPSHOT.json").read_text(encoding="utf-8"))
    for relative, expected in snapshot["files"].items():
        actual = hashlib.sha256((ROOT / "scripts/xhh_sdk" / relative).read_bytes()).hexdigest()
        assert actual == expected, f"SDK snapshot changed: {relative}"
    for document in ("SKILL.md", "REFERENCE.md"):
        text = (ROOT / document).read_text(encoding="utf-8")
        for forbidden in ("C:/Users/", "D:/02_Dev/", "alma-meme", "XHH_RUN_SCOPE", "8899"):
            assert forbidden not in text, (document, forbidden)
    with tempfile.TemporaryDirectory(prefix="xhh-portable-") as temporary:
        folder = Path(temporary)
        skill = folder / "separate skill"
        shutil.copytree(ROOT / "scripts", skill / "scripts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        unrelated = folder / "unrelated cwd"
        unrelated.mkdir()
        environment = {key: value for key, value in os.environ.items() if not key.startswith("XHH_") and key != "PYTHONPATH"}
        environment["XHH_DATA_DIR"] = str(folder / "isolated-data")
        environment["XHH_TEST_SKILL_ROOT"] = str(skill)
        entry = skill / "scripts/xhh.py"
        def invoke(*arguments):
            result = subprocess.run([sys.executable, "-I", "-X", "utf8", str(entry), *arguments],
                    cwd=unrelated, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
            assert result.returncode == 0, (arguments, result.stdout, result.stderr)
            return result.stdout
        assert "comment" in invoke("--help")
        report = json.loads(invoke("--protocol", "web", "doctor", "--offline"))
        assert report["signer_ready"] and not report["java_required"]
        assert not report["account_checked"] and not report["signer_executed"]
        assert "Creator" in invoke("account", "login", "--help")
        assert not (folder / "isolated-data").exists(), "offline discovery wrote state"
        runner = folder / "fixture_runner.py"
        runner.write_text("import runpy,sys\nsys.path.insert(0, " + repr(str(skill / "scripts")) + ")\n"
                          + "runpy.run_path(" + repr(str(ROOT / "tests/test_tools.py")) + ", run_name='__main__')\n", encoding="utf-8")
        test = subprocess.run([sys.executable, "-I", "-X", "utf8", str(runner)], cwd=unrelated,
                env=environment, capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert test.returncode == 0, test.stdout + test.stderr
        print(json.dumps({"ok": True, "sdk_snapshot_verified": len(snapshot["files"]),
                          "unrelated_cwd": True, "isolated_python": True,
                          "web_without_java": True, "offline_state_writes": 0,
                          "fixture_tests": "passed", "live_platform": "not_tested"}))


if __name__ == "__main__":
    main()
