from __future__ import annotations

import base64
import configparser
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import unittest
import venv
import zipfile
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "xiaoheihe_tools"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info")


def offline_environment():
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("XHH_", "PIP_")) and key != "PYTHONPATH"}
    environment.update(PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
                       PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    return environment


def run(arguments, *, cwd, environment=None):
    return subprocess.run([str(value) for value in arguments], cwd=cwd,
                          env=environment or offline_environment(), capture_output=True,
                          text=True, encoding="utf-8", timeout=120)


def checked(arguments, *, cwd):
    result = run(arguments, cwd=cwd)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return result.stdout


def copy_source(root, target):
    target.mkdir()
    for source in [*root.glob("*.md"), *root.glob("requirements-*.txt"), root / "pyproject.toml"]:
        shutil.copy2(source, target / source.name)
    shutil.copytree(root / "scripts", target / "scripts", ignore=IGNORE)


def build_distributions(root, work):
    source = work / "build source"
    copy_source(root, source)
    output = work / "dist"
    output.mkdir()
    checked([sys.executable, "-I", "-c",
             "import sys; from setuptools.build_meta import build_wheel, build_sdist; "
             "output=sys.argv[1]; build_wheel(output); build_sdist(output)", output], cwd=source)
    return next(output.glob("*.whl")), next(output.glob("*.tar.gz"))


def source_manifest(root):
    scripts = root / "scripts"
    return {PACKAGE + "/" + path.relative_to(scripts).as_posix(): path.read_bytes()
            for path in scripts.rglob("*") if path.is_file()
            and "__pycache__" not in path.parts and path.suffix != ".pyc"}


def verify_wheel(root, wheel):
    expected = source_manifest(root)
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)), "duplicate wheel paths"
        actual = {name: archive.read(name) for name in names if name.startswith(PACKAGE + "/")}
        assert actual.keys() == expected.keys(), (
            "wheel file set", sorted(expected.keys() - actual.keys()), sorted(actual.keys() - expected.keys()))
        for name, content in expected.items():
            assert actual[name] == content, f"wheel bytes differ: {name}"
        provenance_name, = [name for name in names if name.endswith(".dist-info/THIRD_PARTY.md")
                           or name.endswith(".dist-info/licenses/THIRD_PARTY.md")]
        assert archive.read(provenance_name) == (root / "THIRD_PARTY.md").read_bytes(), "wheel provenance bytes differ"
        record_name, = [name for name in names if name.endswith(".dist-info/RECORD")]
        records = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
        assert len(records) == len(names) and {row[0] for row in records} == set(names), "wheel RECORD file set"
        for name, digest, size in records:
            if name == record_name:
                assert not digest and not size, "RECORD self digest"
                continue
            content = archive.read(name)
            expected_digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode("ascii")
            assert digest == "sha256=" + expected_digest and size == str(len(content)), f"wheel RECORD mismatch: {name}"
    return {"files": len(expected), "bytes": sum(map(len, expected.values())), "record_verified": True}


def install_wheel(wheel, work):
    folder = work / "environment"
    venv.EnvBuilder(with_pip=True).create(folder)
    python = folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    console = folder / ("Scripts/xhh.exe" if os.name == "nt" else "bin/xhh")
    checked([python, "-I", "-m", "pip", "install", "--no-index", "--no-deps", wheel], cwd=work)
    site = Path(checked([python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], cwd=work).strip())
    assert console.is_file(), "installed console script missing"
    return python, console, site


def verify_install(root, site):
    expected = source_manifest(root)
    installed = site / PACKAGE
    actual = {PACKAGE + "/" + path.relative_to(installed).as_posix(): path.read_bytes()
              for path in installed.rglob("*") if path.is_file()
              and "__pycache__" not in path.parts and path.suffix != ".pyc"}
    assert actual.keys() == expected.keys(), "installed file set differs"
    for name, content in expected.items():
        assert actual[name] == content, f"installed bytes differ: {name}"
    return len(actual)


def equivalent_smoke(root, console, site, work, *, installed_entry=None):
    unrelated = work / "unrelated cwd"
    unrelated.mkdir()
    (unrelated / "operations.py").write_text("raise RuntimeError('unrelated CWD imported')\n", encoding="utf-8")
    data = work / "offline state"
    environment = offline_environment()
    environment["XHH_DATA_DIR"] = str(data)
    entry = root / "scripts/xhh.py"
    text = work / "preview text.json"
    text.write_text(json.dumps({"content": "第一行\n第二行", "content_format": "text", "original": False},
                               ensure_ascii=False), encoding="utf-8")
    html = work / "preview html.json"
    html.write_text(json.dumps({"content": "<p>离线 HTML</p>", "post_format": "article", "content_format": "html"},
                               ensure_ascii=False), encoding="utf-8")
    cases = [("--help",), ("--protocol", "web", "doctor", "--offline"), ("describe",),
             ("preview-post", str(text)), ("preview-post", str(html)),
             ("describe", "--command", "compose"), ("preview-post", str(work / "missing.json"))]
    for arguments in cases:
        direct = run([sys.executable, "-I", "-S", "-B", "-X", "utf8", entry, *arguments],
                     cwd=unrelated, environment=environment)
        installed = run([console, *arguments], cwd=unrelated, environment=environment)
        expected_code = 1 if arguments[-1].endswith("missing.json") else 0
        assert direct.returncode == installed.returncode == expected_code, (arguments, direct, installed)
        assert direct.stderr == installed.stderr == "", (arguments, direct.stderr, installed.stderr)
        if arguments == ("--help",):
            assert direct.stdout == installed.stdout, "help differs"
            assert "preview-post" in installed.stdout
            continue
        left, right = json.loads(direct.stdout), json.loads(installed.stdout)
        if arguments[0] == "describe":
            for payload, expected_entry in ((left, entry), (right, installed_entry or site / PACKAGE / "xhh.py")):
                located = Path(payload["data"]["entrypoint_file"])
                assert located.is_absolute() and located.resolve() == expected_entry.resolve(), "describe entrypoint location"
                assert located.samefile(expected_entry), "describe entrypoint file identity"
                assert located.read_bytes() == entry.read_bytes(), "describe entrypoint bytes"
                payload["data"]["entrypoint_file"] = "<validated-entrypoint>"
        assert left == right, (arguments, left, right)
        if arguments[0] == "preview-post" and expected_code == 0:
            assert right["ok"] and right["coverage"]["network"] == "not_used"
            assert set(right) == {"version", "ok", "kind", "data", "coverage", "error"}
        if arguments[0] == "--protocol":
            assert right["signer_ready"] and not right["java_required"]
            assert not right["account_checked"] and not right["signer_executed"]
    assert not data.exists(), "offline commands wrote state"
    return {"calls": len(cases), "exit_codes_and_json_equal": True,
            "entrypoint_file": "absolute location and source bytes verified before substitution",
            "offline_state_writes": 0}


class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="xhh-packaging-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.work = Path(cls.temporary.name)
        cls.wheel, cls.sdist = build_distributions(ROOT, cls.work)
        cls.python, cls.console, cls.site = install_wheel(cls.wheel, cls.work)

    def test_project_metadata_and_optional_dependencies(self):
        from pip._vendor.packaging.requirements import Requirement

        def requirements(path):
            result = set()
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("-r "):
                    result.update(requirements(path.parent / line[3:]))
                elif line.strip() and not line.startswith("#"):
                    item = Requirement(line)
                    result.add((item.name.lower().replace("_", "-"), str(item.specifier)))
            return result

        with zipfile.ZipFile(self.wheel) as archive:
            name, = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            metadata = BytesParser().parsebytes(archive.read(name))
            self.assertEqual(metadata["Name"], "xiaoheihe-tools")
            self.assertEqual(metadata["Requires-Python"], ">=3.10")
            dependencies = metadata.get_all("Requires-Dist", [])
            self.assertTrue(all("extra ==" in item for item in dependencies), dependencies)
            self.assertEqual(set(metadata.get_all("Provides-Extra")), {"formatting", "media", "test"})
            for extra in ("formatting", "media", "test"):
                actual = set()
                for value in dependencies:
                    item = Requirement(value)
                    if item.marker.evaluate({"extra": extra}):
                        actual.add((item.name.lower().replace("_", "-"), str(item.specifier)))
                self.assertEqual(actual, requirements(ROOT / ("requirements-" + extra + ".txt")))
            self.assertFalse(any("playwright" in item.lower() for item in dependencies))
            name, = [name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")]
            entries = configparser.ConfigParser()
            entries.read_string(archive.read(name).decode("utf-8"))
            self.assertEqual(entries["console_scripts"]["xhh"], "xiaoheihe_tools.xhh:console_main")
        checked([self.python, "-I", "-c",
                 "import importlib.metadata as m; d=m.distribution('xiaoheihe-tools'); "
                 "assert not [x for x in (d.requires or []) if 'extra ==' not in x]"], cwd=self.work)

    def test_wheel_and_installed_file_bytes_match_current_source(self):
        self.assertGreater(verify_wheel(ROOT, self.wheel)["files"], 30)
        self.assertEqual(verify_install(ROOT, self.site), len(source_manifest(ROOT)))

    def test_installed_console_and_isolated_direct_entry_are_equivalent(self):
        equivalent_smoke(ROOT, self.console, self.site, self.work)

    def test_sdist_can_build_and_install_offline(self):
        source = self.work / "sdist source"
        source.mkdir()
        with tarfile.open(self.sdist) as archive:
            # Only extract the locally built archive after validating its member paths.
            for member in archive.getmembers():
                path = Path(member.name)
                self.assertFalse(path.is_absolute() or ".." in path.parts or member.issym() or member.islnk())
            if hasattr(tarfile, "data_filter"):
                archive.extractall(source, filter="data")
            else:
                archive.extractall(source)
        unpacked, = source.iterdir()
        output = self.work / "sdist wheel"
        output.mkdir()
        checked([sys.executable, "-I", "-c", "import sys; from setuptools.build_meta import build_wheel; build_wheel(sys.argv[1])",
                 output], cwd=unpacked)
        wheel, = output.glob("*.whl")
        verify_wheel(ROOT, wheel)
        install = self.work / "sdist install"
        install.mkdir()
        python, console, site = install_wheel(wheel, install)
        backend_path = site / "_test_build_environment.pth"
        backend_path.write_text(sysconfig.get_path("purelib") + "\n", encoding="utf-8")
        checked([python, "-I", "-m", "pip", "install", "--no-index", "--no-build-isolation",
                 "--no-deps", "--force-reinstall", self.sdist], cwd=install)
        backend_path.unlink()
        verify_install(ROOT, site)
        equivalent_smoke(ROOT, console, site, install)

    def test_editable_console_uses_the_same_source(self):
        work = self.work / "editable"
        work.mkdir()
        python, console, site = install_wheel(self.wheel, work)
        (site / "_test_build_environment.pth").write_text(sysconfig.get_path("purelib") + "\n", encoding="utf-8")
        # Exercise a valid alias spelling even on hosts without Windows short paths.
        source = self.work / "build source" / ".." / "build source"
        checked([python, "-I", "-m", "pip", "install", "--no-index", "--no-build-isolation", "--no-deps", "-e", source], cwd=work)
        described = json.loads(checked([console, "--data-dir", work / "no state", "describe"], cwd=work))
        located = Path(described["data"]["entrypoint_file"])
        expected = source / "scripts/xhh.py"
        self.assertTrue(located.is_absolute())
        self.assertEqual(located.resolve(), expected.resolve())
        self.assertTrue(located.samefile(expected))
        self.assertFalse((work / "no state").exists())
        equivalent_smoke(source, console, site, work, installed_entry=source / "scripts/xhh.py")

    def test_provenance_negative_controls(self):
        from verify_portable import verify_provenance
        source = self.work / "provenance source"
        copy_source(ROOT, source)
        sdk = source / "scripts/xhh_sdk"
        snapshot_file = sdk / "SNAPSHOT.json"
        original = snapshot_file.read_bytes()
        snapshot = json.loads(original)
        for change in ("invalid_json", "traversal", "absolute", "windows_path", "invalid_hash", "invalid_structure"):
            with self.subTest(change=change):
                altered = json.loads(original)
                if change == "invalid_json":
                    snapshot_file.write_text("{", encoding="utf-8")
                else:
                    if change in ("traversal", "absolute", "windows_path"):
                        key = {"traversal": "../outside.py", "absolute": "/outside.py", "windows_path": "C:\\outside.py"}[change]
                        altered["files"][key] = "0" * 64
                    elif change == "invalid_hash":
                        altered["files"]["accounts.py"] = "invalid"
                    else:
                        altered["files"] = []
                    snapshot_file.write_text(json.dumps(altered), encoding="utf-8")
                with self.assertRaises((AssertionError, json.JSONDecodeError)):
                    verify_provenance(source)
                snapshot_file.write_bytes(original)
        target = sdk / "accounts.py"
        content = target.read_bytes()
        target.unlink()
        with self.assertRaisesRegex(AssertionError, "historical source file missing"):
            verify_provenance(source)
        target.write_bytes(content)
        license_file = sdk / "LICENSE"
        license_file.write_bytes(license_file.read_bytes() + b"damaged")
        with self.assertRaisesRegex(AssertionError, "SDK license bytes changed"):
            verify_provenance(source)
        snapshot["files"]["LICENSE"] = hashlib.sha256(license_file.read_bytes()).hexdigest()
        snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "historical license digest changed"):
            verify_provenance(source)

    def test_missing_and_corrupt_wheel_files_are_rejected(self):
        target = PACKAGE + "/xhh_sdk/accounts.py"
        errors = {"missing": "wheel file set", "corrupt": "wheel bytes differ",
                  "record_corrupt": "wheel RECORD mismatch", "record_missing": "wheel RECORD file set",
                  "provenance_corrupt": "wheel provenance bytes differ"}
        for change, message in errors.items():
            with self.subTest(change=change):
                wheel = self.work / (change + ".whl")
                with zipfile.ZipFile(self.wheel) as original, zipfile.ZipFile(wheel, "w") as altered:
                    for name in original.namelist():
                        if name == target and change == "missing":
                            continue
                        content = original.read(name)
                        if name == target and change == "corrupt":
                            content += b"\n# damaged\n"
                        if name.endswith("/THIRD_PARTY.md") and change == "provenance_corrupt":
                            content += b"damaged"
                        if name.endswith(".dist-info/RECORD") and change.startswith("record_"):
                            rows = list(csv.reader(io.StringIO(content.decode("utf-8"))))
                            if change == "record_missing":
                                rows = [row for row in rows if row[0] != target]
                            else:
                                for row in rows:
                                    if row[0] == target:
                                        row[1] = "sha256=damaged"
                            buffer = io.StringIO()
                            csv.writer(buffer).writerows(rows)
                            content = buffer.getvalue().encode("utf-8")
                        altered.writestr(name, content)
                with self.assertRaisesRegex(AssertionError, message):
                    verify_wheel(ROOT, wheel)

    def test_installed_corruption_and_omission_are_rejected(self):
        target = self.site / PACKAGE / "xhh_sdk/accounts.py"
        content = target.read_bytes()
        try:
            target.write_bytes(content + b"\n# damaged\n")
            with self.assertRaisesRegex(AssertionError, "installed bytes differ"):
                verify_install(ROOT, self.site)
            target.unlink()
            with self.assertRaisesRegex(AssertionError, "installed file set differs"):
                verify_install(ROOT, self.site)
        finally:
            target.write_bytes(content)

    def test_legal_sdk_evolution_uses_current_source_not_historical_hashes(self):
        work = self.work / "evolution"
        work.mkdir()
        source = work / "source"
        copy_source(ROOT, source)
        target = source / "scripts/xhh_sdk/accounts.py"
        target.write_bytes(target.read_bytes() + b"\n# synthetic legal SDK evolution\n")
        (source / "scripts/xhh_sdk/evolution_probe.py").write_bytes(b"VALUE = 'synthetic new SDK module'\n")
        from verify_portable import verify_provenance
        verify_provenance(source)
        wheel, _ = build_distributions(source, work)
        self.assertEqual(verify_wheel(source, wheel)["files"], len(source_manifest(ROOT)) + 1)
        with self.assertRaisesRegex(AssertionError, "wheel file set"):
            verify_wheel(ROOT, wheel)


if __name__ == "__main__":
    unittest.main()
