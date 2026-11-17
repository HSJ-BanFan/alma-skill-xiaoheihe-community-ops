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
sys.path.insert(0, str(ROOT / "tests"))

import verify_skill_docs

COPY_FOLDERS = ("scripts", "tests", "references", "docs")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def copy_skill(target: Path) -> None:
    for source in sorted([*ROOT.glob("*.md"), *ROOT.glob("requirements-*.txt")]):
        shutil.copy2(source, target / source.name)
    for name in COPY_FOLDERS:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, target / name, ignore=IGNORE)


def main():
    snapshot = json.loads((ROOT / "scripts/xhh_sdk/SNAPSHOT.json").read_text(encoding="utf-8"))
    for relative, expected in snapshot["files"].items():
        actual = hashlib.sha256((ROOT / "scripts/xhh_sdk" / relative).read_bytes()).hexdigest()
        assert actual == expected, f"SDK snapshot changed: {relative}"
    for document in sorted(ROOT.glob("*.md")):
        text = document.read_text(encoding="utf-8")
        for forbidden in ("C:/Users/", "D:/02_Dev/", "alma-meme", "XHH_RUN_SCOPE", "8899"):
            assert forbidden not in text, (document.name, forbidden)
    for document in sorted((ROOT / "references").glob("*.md")):
        text = document.read_text(encoding="utf-8")
        for forbidden in ("C:/Users/", "D:/02_Dev/", "alma-meme", "XHH_RUN_SCOPE", "8899"):
            assert forbidden not in text, (document.name, forbidden)
    with tempfile.TemporaryDirectory(prefix="xhh-portable-") as temporary:
        folder = Path(temporary)
        skill = folder / "separate skill"
        skill.mkdir()
        copy_skill(skill)
        unrelated = folder / "unrelated cwd"
        unrelated.mkdir()
        specs = folder / "preview specs"
        specs.mkdir()

        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("XHH_") and key != "PYTHONPATH"}
        environment["XHH_DATA_DIR"] = str(folder / "isolated-data")
        environment["XHH_TEST_SKILL_ROOT"] = str(skill)
        entry = skill / "scripts/xhh.py"

        def invoke_result(*arguments):
            return subprocess.run([sys.executable, "-I", "-S", "-X", "utf8", str(entry), *arguments],
                                  cwd=unrelated, env=environment, capture_output=True, text=True,
                                  encoding="utf-8", timeout=30)

        def invoke(*arguments):
            result = invoke_result(*arguments)
            assert result.returncode == 0, (arguments, result.stdout, result.stderr)
            return result.stdout

        assert "comment" in invoke("--help")
        assert "preview-post" in invoke("--help")
        report = json.loads(invoke("--protocol", "web", "doctor", "--offline"))
        assert report["signer_ready"] and not report["java_required"]
        assert not report["account_checked"] and not report["signer_executed"]
        assert "Creator" in invoke("account", "login", "--help")

        text_spec = specs / "text post.json"
        text_spec.write_text(json.dumps({"content": "第一行\n第二行", "content_format": "text",
                                         "original": False}, ensure_ascii=False), encoding="utf-8")
        html_spec = specs / "html post.json"
        html_spec.write_text(json.dumps({"content": "<p>已有 HTML</p>", "post_format": "article", "content_format": "html",
                                         "original": False}, ensure_ascii=False), encoding="utf-8")
        preview = json.loads(invoke("preview-post", str(text_spec)))
        assert preview["ok"] and json.loads(preview["data"]["payload"]["text"])[0] == {"type": "text", "text": "第一行\n第二行"}
        assert preview["coverage"]["network"] == "not_used"
        preview = json.loads(invoke("preview-post", str(html_spec)))
        assert preview["ok"] and "<p>已有 HTML</p>" in preview["data"]["payload"]["text"]

        markdown_spec = specs / "markdown post.json"
        markdown_spec.write_text(json.dumps({"content": "# 标题", "post_format": "article", "content_format": "markdown"},
                                            ensure_ascii=False), encoding="utf-8")
        missing = invoke_result("preview-post", str(markdown_spec))
        assert missing.returncode == 1, missing.stdout
        diagnostic = json.loads(missing.stdout)
        assert diagnostic["ok"] is False
        assert "requirements-formatting.txt" in diagnostic["error"]["message"]

        docs = verify_skill_docs.check(skill)
        assert docs["ok"], docs
        assert not (folder / "isolated-data").exists(), "offline discovery wrote state"
        test = subprocess.run([sys.executable, "-I", "-X", "utf8", "-m", "unittest", "discover",
                               "-s", str(skill / "tests"), "-q"], cwd=unrelated, env=environment,
                              capture_output=True, text=True, encoding="utf-8", timeout=300)
        assert test.returncode == 0, test.stdout + test.stderr

        (skill / "references/formatting.md").unlink()
        negative = verify_skill_docs.check(skill)
        assert not negative["ok"]
        assert any(item["target"] == "references/formatting.md"
                   for item in negative["broken_links"])
        print(json.dumps({"ok": True, "sdk_snapshot_verified": len(snapshot["files"]),
                          "unrelated_cwd": True, "isolated_python": True,
                          "web_without_java": True, "offline_state_writes": 0,
                          "preview_formats": ["text", "html"], "markdown_missing_diagnostic": True,
                          "documents_copied": docs["documents"], "doc_links_checked": True,
                          "doc_link_negative_control": True, "fixture_tests": "passed",
                          "live_platform": "not_tested"}))


if __name__ == "__main__":
    main()
