from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import verify_skill_docs
from test_packaging import build_distributions, equivalent_smoke, install_wheel, verify_install, verify_wheel

COPY_FOLDERS = ("scripts", "tests", "references", "docs")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")
SDK_LICENSE_SHA256 = "6ad220b8dbff857ab6579903d8d4d5912bab9f99e0e03f63d317c04fbe6544c5"


def safe_relative(value):
    assert isinstance(value, str) and value, "provenance path must be a nonempty string"
    path = PurePosixPath(value)
    assert not path.is_absolute() and ".." not in path.parts and not any(
        character in value for character in "\\:\x00"), f"unsafe provenance path: {value}"
    assert path.as_posix() == value and value != ".", f"noncanonical provenance path: {value}"
    return path


def verify_provenance(root):
    sdk = root / "scripts/xhh_sdk"
    snapshot = json.loads((sdk / "SNAPSHOT.json").read_text(encoding="utf-8"))
    assert isinstance(snapshot, dict), "provenance must be an object"
    assert snapshot.get("repository") == "https://github.com/HSJ-BanFan/xiaoheihe-api-collect", "historical repository"
    assert re.fullmatch(r"[0-9a-f]{40}", snapshot.get("revision", "")), "historical revision"
    safe_relative(snapshot.get("source_path"))
    files = snapshot.get("files")
    assert isinstance(files, dict) and files and "LICENSE" in files, "historical file map"
    for relative, digest in files.items():
        path = sdk / safe_relative(relative)
        assert path.resolve().is_relative_to(sdk.resolve()), "provenance path escapes SDK"
        assert path.is_file(), f"historical source file missing: {relative}"
        assert isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest), "historical SHA-256 shape"
    patches = snapshot.get("downstream_patches", {})
    assert isinstance(patches, dict), "historical patches must be an object"
    for relative, description in patches.items():
        safe_relative(relative)
        assert relative in files and isinstance(description, str) and description, "historical patch entry"
    assert files["LICENSE"] == SDK_LICENSE_SHA256, "historical license digest changed"
    assert hashlib.sha256((sdk / "LICENSE").read_bytes()).hexdigest() == SDK_LICENSE_SHA256, "SDK license bytes changed"
    assert (root / "THIRD_PARTY.md").is_file(), "third-party provenance missing"
    return {"historical_files": len(files), "license_sha256_verified": True,
            "sdk_python_hashes": "historical provenance, not an evolution constraint"}


def copy_skill(target: Path) -> None:
    for source in sorted([*ROOT.glob("*.md"), *ROOT.glob("requirements-*.txt"), ROOT / "pyproject.toml"]):
        shutil.copy2(source, target / source.name)
    for name in COPY_FOLDERS:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, target / name, ignore=IGNORE)


def main():
    provenance = verify_provenance(ROOT)
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
        artifacts = folder / "artifacts"
        artifacts.mkdir()
        wheel, sdist = build_distributions(skill, artifacts)
        integrity = verify_wheel(skill, wheel)
        installation = install_wheel(wheel, artifacts)
        integrity["installation"] = verify_install(skill, installation)
        smoke = equivalent_smoke(skill, installation.console, installation.site, artifacts)
        test = subprocess.run([sys.executable, "-I", "-X", "utf8", "-m", "unittest", "discover",
                               "-s", str(skill / "tests"), "-q"], cwd=unrelated, env=environment,
                              capture_output=True, text=True, encoding="utf-8", timeout=300)
        assert test.returncode == 0, test.stdout + test.stderr

        (skill / "references/formatting.md").unlink()
        negative = verify_skill_docs.check(skill)
        assert not negative["ok"]
        assert any(item["target"] == "references/formatting.md"
                   for item in negative["broken_links"])
        print(json.dumps({"ok": True, "sdk_provenance": provenance,
                          "current_artifact_integrity": integrity,
                          "installed_entrypoint_equivalence": smoke,
                          "sdist_built": sdist.is_file(),
                          "integrity_controls": ["legal_sdk_evolution", "wheel_omission", "wheel_corruption",
                                                 "wheel_record_omission", "wheel_record_corruption", "wheel_provenance_corruption",
                                                 "installed_omission", "installed_corruption", "wheel_runtime_injection",
                                                 "wheel_archive_path_safety", "wheel_duplicate_and_special_members",
                                                 "wheel_generated_metadata_corruption",
                                                 "installed_external_runtime", "installed_record_closure", "installed_launcher_corruption",
                                                 "historical_path_safety", "license_corruption"],
                          "unrelated_cwd": True, "isolated_python": True,
                          "web_without_java": True, "offline_state_writes": 0,
                          "preview_formats": ["text", "html"], "markdown_missing_diagnostic": True,
                          "documents_copied": docs["documents"], "doc_links_checked": True,
                          "doc_link_negative_control": True, "fixture_tests": "passed",
                          "live_platform": "not_tested"}))


if __name__ == "__main__":
    main()
