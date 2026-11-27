from __future__ import annotations

import base64
import configparser
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import unittest
import venv
import warnings
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "xiaoheihe_tools"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info")


def offline_environment():
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("XHH_", "PIP_")) and key != "PYTHONPATH"}
    environment.update(PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
                       PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", SOURCE_DATE_EPOCH="315532800")
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


def write_recorded_wheel(original, target, additions):
    """Synthetic mutations with valid hashes, so RECORD alone cannot reject them."""
    with zipfile.ZipFile(original) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    record, = [name for name in contents if name.endswith(".dist-info/RECORD")]
    contents.update(additions)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for name, content in contents.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode("ascii")
        writer.writerow((name, "" if name == record else "sha256=" + digest,
                         "" if name == record else str(len(content))))
    contents[record] = buffer.getvalue().encode("utf-8")
    with zipfile.ZipFile(target, "w") as archive:
        for name, content in contents.items():
            member = zipfile.ZipInfo("placeholder")
            member.filename = member.orig_filename = name
            archive.writestr(member, content)


def safe_wheel_path(name, *, label="wheel"):
    # Check raw spelling before pathlib can collapse empty or dot components.
    assert name and not any(c in name for c in "\\:\x00"), f"unsafe {label} path: {name}"
    assert all(part not in ("", ".", "..") and not part.endswith((".", " "))
               for part in name.split("/")), f"unsafe {label} path: {name}"


def wheel_contents(wheel):
    with zipfile.ZipFile(wheel) as archive:
        seen = set()
        contents = {}
        for member in archive.infolist():
            name = member.filename
            safe_wheel_path(member.orig_filename)
            safe_wheel_path(name)
            assert name.casefold() not in seen, f"duplicate wheel path: {name}"
            seen.add(name.casefold())
            mode = stat.S_IFMT(member.external_attr >> 16)
            assert mode in (0, stat.S_IFREG) and not member.is_dir(), f"unsafe wheel member type: {name}"
            contents[name] = archive.read(member)
        return contents


METADATA_CACHE = {}


def metadata_reference(root):
    """Build metadata from a clean source copy, never from candidate RECORD claims."""
    inputs = source_manifest(root)
    for path in [*root.glob("*.md"), *root.glob("requirements-*.txt"), root / "pyproject.toml"]:
        inputs[path.name] = path.read_bytes()
    key = tuple((name, hashlib.sha256(content).digest()) for name, content in sorted(inputs.items()))
    if key not in METADATA_CACHE:
        with tempfile.TemporaryDirectory(prefix="xhh-metadata-") as temporary:
            work = Path(temporary)
            source = work / "source"
            copy_source(root, source)
            output = work / "dist"
            output.mkdir()
            checked([sys.executable, "-I", "-B", "-c",
                     "import sys; from setuptools.build_meta import build_wheel; build_wheel(sys.argv[1])",
                     output], cwd=source)
            wheel, = output.glob("*.whl")
            reference = wheel_contents(wheel)
        metadata, = [name for name in reference if name.endswith(".dist-info/METADATA")]
        prefix = metadata.removesuffix("METADATA")
        allowed = {prefix + name for name in ("METADATA", "WHEEL", "entry_points.txt", "top_level.txt", "RECORD")}
        licenses = {prefix + "licenses/THIRD_PARTY.md", prefix + "licenses/scripts/xhh_sdk/LICENSE"}
        if not licenses.issubset(reference):
            licenses = {prefix + "THIRD_PARTY.md", prefix + "LICENSE"}
        allowed.update(licenses)
        actual = {name for name in reference if not name.startswith(PACKAGE + "/")}
        assert actual == allowed, ("unsupported generated metadata file set", sorted(actual ^ allowed))
        METADATA_CACHE[key] = {name: reference[name] for name in allowed if not name.endswith("/RECORD")}
    return METADATA_CACHE[key]


def verify_record(contents, record_name, *, label):
    rows = list(csv.reader(io.StringIO(contents[record_name].decode("utf-8"))))
    assert all(len(row) == 3 for row in rows), f"{label} RECORD structure"
    assert len(rows) == len(contents) and {row[0] for row in rows} == set(contents), f"{label} RECORD file set"
    for name, digest, size in rows:
        if name == record_name:
            assert not digest and not size, "RECORD self digest"
        else:
            content = contents[name]
            expected = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode("ascii")
            assert digest == "sha256=" + expected and size == str(len(content)), f"{label} RECORD mismatch: {name}"


def verify_wheel(root, wheel):
    expected = source_manifest(root)
    contents = wheel_contents(wheel)
    actual = {name: content for name, content in contents.items() if name.startswith(PACKAGE + "/")}
    assert actual.keys() == expected.keys(), (
        "wheel file set", sorted(expected.keys() - actual.keys()), sorted(actual.keys() - expected.keys()))
    for name, content in expected.items():
        assert actual[name] == content, f"wheel bytes differ: {name}"
    metadata = metadata_reference(root)
    record_name, = [name.removesuffix("METADATA") + "RECORD" for name in metadata if name.endswith("/METADATA")]
    allowed = expected.keys() | metadata.keys() | {record_name}
    for name in contents.keys() - allowed:
        raise AssertionError(f"unexpected wheel member: {name}")
    assert contents.keys() == allowed, ("wheel metadata file set", sorted(allowed - contents.keys()))
    for name, content in metadata.items():
        message = "wheel provenance bytes differ" if name.endswith("/THIRD_PARTY.md") else f"wheel metadata bytes differ: {name}"
        assert contents[name] == content, message
    verify_record(contents, record_name, label="wheel")
    return {"files": len(expected), "bytes": sum(map(len, expected.values())),
            "record_verified": True, "all_members_closed": len(contents)}


def environment_manifest(folder):
    result = {}
    for path in folder.rglob("*"):
        name = path.relative_to(folder).as_posix()
        if path.is_symlink():
            result[name] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[name] = ("file", path.read_bytes())
    return result


@dataclass
class Installation:
    folder: Path
    python: Path
    console: Path
    site: Path
    wheel: Path
    artifact: Path
    baseline: dict
    launcher: bytes


def install_wheel(wheel, work):
    # Keep venv shebangs stable across Windows TEMP 8.3 spellings and symlink aliases.
    folder = (work / "environment").resolve()
    venv.EnvBuilder(with_pip=True).create(folder)
    python = folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    console = folder / ("Scripts/xhh.exe" if os.name == "nt" else "bin/xhh")
    site = Path(checked([python, "-I", "-B", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], cwd=work).strip())
    # Generate the expected launcher with the trusted seeded pip before candidate installation.
    launcher_dir = work / "expected launcher"
    launcher_dir.mkdir()
    checked([python, "-I", "-B", "-c",
             "import sys; from pip._internal.operations.install.wheel import PipScriptMaker; "
             "maker=PipScriptMaker(None, sys.argv[1]); maker.clobber=True; maker.variants={''}; "
             "maker.set_mode=True; "
             "maker.make('xhh = xiaoheihe_tools.xhh:console_main')", launcher_dir], cwd=work)
    launcher = (launcher_dir / console.name).read_bytes()
    baseline = environment_manifest(folder)
    checked([python, "-I", "-B", "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile", wheel], cwd=work)
    assert console.is_file(), "installed console script missing"
    return Installation(folder, python, console, site, wheel, wheel, baseline, launcher)


def verify_install(root, installation):
    verify_wheel(root, installation.wheel)
    contents = wheel_contents(installation.wheel)
    record_name, = [name for name in contents if name.endswith(".dist-info/RECORD")]
    prefix = record_name.removesuffix("RECORD")
    site_prefix = installation.site.relative_to(installation.folder).as_posix() + "/"
    expected = {site_prefix + name: ("file", content) for name, content in contents.items() if name != record_name}
    expected[site_prefix + prefix + "INSTALLER"] = ("file", b"pip\n")
    expected[site_prefix + prefix + "REQUESTED"] = ("file", b"")
    console_name = installation.console.relative_to(installation.folder).as_posix()
    expected[console_name] = ("file", installation.launcher)
    # These two files vary by installation location and are validated below.
    generated = {site_prefix + record_name, site_prefix + prefix + "direct_url.json"}
    actual = environment_manifest(installation.folder)
    allowed = installation.baseline.keys() | expected.keys() | generated
    for name in sorted(actual.keys() - allowed):
        raise AssertionError(f"unexpected installed file: {name}")
    assert actual.keys() == allowed, ("installed file set differs", sorted(allowed - actual.keys()))
    for name, value in (installation.baseline | expected).items():
        assert actual[name] == value, f"installed bytes differ: {name}"
    for name in generated:
        assert actual[name][0] == "file", f"unsafe installed file type: {name}"
    direct_url = json.loads(actual[site_prefix + prefix + "direct_url.json"][1])
    assert set(direct_url) == {"url", "archive_info"}, "installed direct URL structure"
    assert direct_url["url"] == installation.artifact.resolve().as_uri(), "installed direct URL origin"
    digest = hashlib.sha256(installation.artifact.read_bytes()).hexdigest()
    info = direct_url["archive_info"]
    assert info in ({"hash": "sha256=" + digest, "hashes": {"sha256": digest}},
                    {"hashes": {"sha256": digest}}), "installed archive digest"
    installed_contents = {name: actual[site_prefix + name][1] for name in contents}
    for name in ("INSTALLER", "REQUESTED", "direct_url.json"):
        installed_contents[prefix + name] = actual[site_prefix + prefix + name][1]
    launcher_path = os.path.relpath(installation.console, installation.site).replace(os.sep, "/")
    installed_contents[launcher_path] = installation.launcher
    # Exact RECORD set admits only this validated launcher path outside site-packages.
    for row in csv.reader(io.StringIO(installed_contents[record_name].decode("utf-8"))):
        assert len(row) == 3, "installed RECORD structure"
        name = row[0]
        if name != launcher_path:
            safe_wheel_path(name, label="installed RECORD")
        assert name in installed_contents, f"unexpected installed RECORD member: {name}"
    verify_record(installed_contents, record_name, label="installed")
    assert os.access(installation.console, os.X_OK), "installed launcher is not executable"
    return {"files": len(source_manifest(root)), "owned_files": len(expected) + len(generated),
            "baseline_files_unchanged": len(installation.baseline), "whole_environment_verified": True}


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
             ("describe", "--command", "compose"), ("describe", "--command", "emojis get"),
             ("preview-post", str(work / "missing.json"))]
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
        cls.installation = install_wheel(cls.wheel, cls.work)
        cls.python, cls.console, cls.site = cls.installation.python, cls.installation.console, cls.installation.site

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
        self.assertEqual(verify_install(ROOT, self.installation)["files"], len(source_manifest(ROOT)))

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
        installation = install_wheel(wheel, install)
        python, console, site = installation.python, installation.console, installation.site
        backend_path = site / "_test_build_environment.pth"
        backend_path.write_text(sysconfig.get_path("purelib") + "\n", encoding="utf-8")
        checked([python, "-I", "-B", "-m", "pip", "install", "--no-index", "--no-build-isolation",
                 "--no-deps", "--no-compile", "--force-reinstall", self.sdist], cwd=install)
        backend_path.unlink()
        installation.artifact = self.sdist
        verify_install(ROOT, installation)
        equivalent_smoke(ROOT, console, site, install)

    def test_editable_console_uses_the_same_source(self):
        work = self.work / "editable"
        work.mkdir()
        installation = install_wheel(self.wheel, work)
        python, console, site = installation.python, installation.console, installation.site
        backend_path = site / "_test_build_environment.pth"
        backend_path.write_text(sysconfig.get_path("purelib") + "\n", encoding="utf-8")
        # Exercise a valid alias spelling even on hosts without Windows short paths.
        source = self.work / "build source" / ".." / "build source"
        checked([python, "-I", "-B", "-m", "pip", "install", "--no-index", "--no-build-isolation", "--no-deps",
                 "--no-compile", "-e", source], cwd=work)
        backend_path.unlink()
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
                verify_install(ROOT, self.installation)
            target.unlink()
            with self.assertRaisesRegex(AssertionError, "installed file set differs"):
                verify_install(ROOT, self.installation)
        finally:
            target.write_bytes(content)

    def test_recorded_extra_wheel_runtime_is_rejected(self):
        for name in ("xiaoheihe_tools-0.1.0.data/purelib/sitecustomize.py", "sitecustomize.py",
                     "extra.pth", "xiaoheihe_tools-0.1.0.dist-info/extra.py"):
            with self.subTest(member=name):
                wheel = self.work / "runtime-injection.whl"
                write_recorded_wheel(self.wheel, wheel, {name: b"# synthetic runtime injection\n"})
                with self.assertRaisesRegex(AssertionError, "unexpected wheel member: " + re.escape(name)):
                    verify_wheel(ROOT, wheel)

    def test_recorded_generated_metadata_changes_are_rejected(self):
        with zipfile.ZipFile(self.wheel) as archive:
            names = [name for name in archive.namelist() if name.endswith(("/WHEEL", "/entry_points.txt", "/METADATA"))]
            contents = {name: archive.read(name) for name in names}
        for name, content in contents.items():
            with self.subTest(member=name):
                wheel = self.work / "metadata-injection.whl"
                write_recorded_wheel(self.wheel, wheel, {name: content + b"\nSynthetic-Alteration: injected\n"})
                with self.assertRaisesRegex(AssertionError, "wheel metadata bytes differ: " + re.escape(name)):
                    verify_wheel(ROOT, wheel)

    def test_recorded_unsafe_wheel_paths_are_rejected(self):
        for name in ("../synthetic-escape.txt", "/synthetic-absolute.txt", "C:/synthetic-drive.txt",
                     "..\\synthetic-escape.txt", "C:\\synthetic-drive.txt", "a//b.py", "a/./b.py", "a/../b.py",
                     "a\x00b.py"):
            with self.subTest(member=name):
                wheel = self.work / "unsafe-path.whl"
                write_recorded_wheel(self.wheel, wheel, {name: b"synthetic unsafe path\n"})
                with self.assertRaisesRegex(AssertionError, "unsafe wheel path: " + re.escape(name)):
                    verify_wheel(ROOT, wheel)

    def test_empty_duplicate_and_special_wheel_members_are_rejected(self):
        wheel = self.work / "invalid-member.whl"
        write_recorded_wheel(self.wheel, wheel, {"": b"synthetic empty member"})
        with self.assertRaisesRegex(AssertionError, "unsafe wheel path: $"):
            verify_wheel(ROOT, wheel)
        target = PACKAGE + "/xhh.py"
        write_recorded_wheel(self.wheel, wheel, {target.upper(): b"# synthetic case alias\n"})
        with self.assertRaisesRegex(AssertionError, "duplicate wheel path: " + re.escape(target.upper())):
            verify_wheel(ROOT, wheel)
        shutil.copyfile(self.wheel, wheel)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(wheel, "a") as archive:
                archive.writestr(target, archive.read(target))
        with self.assertRaisesRegex(AssertionError, "duplicate wheel path: " + re.escape(target)):
            verify_wheel(ROOT, wheel)
        for mode in (stat.S_IFLNK, stat.S_IFDIR, stat.S_IFIFO):
            with self.subTest(mode=mode):
                shutil.copyfile(self.wheel, wheel)
                with zipfile.ZipFile(wheel, "a") as archive:
                    member = zipfile.ZipInfo("synthetic-special")
                    member.create_system = 3
                    member.external_attr = (mode | 0o644) << 16
                    archive.writestr(member, b"synthetic")
                with self.assertRaisesRegex(AssertionError, "unsafe wheel member type: synthetic-special"):
                    verify_wheel(ROOT, wheel)

    def test_installed_external_runtime_is_rejected(self):
        for name in ("sitecustomize.py", "extra.pth"):
            with self.subTest(member=name):
                target = self.site / name
                try:
                    target.write_bytes(b"# synthetic external runtime\n")
                    with self.assertRaisesRegex(AssertionError, "unexpected installed file: .*" + re.escape(name)):
                        verify_install(ROOT, self.installation)
                finally:
                    target.unlink()

    def test_installed_record_and_launcher_are_closed(self):
        record, = self.site.glob(PACKAGE + "-*.dist-info/RECORD")
        original = record.read_bytes()
        for name, message in (("extra.py", "unexpected installed RECORD member: extra.py"),
                              ("../synthetic-escape.py", "unsafe installed RECORD path: ../synthetic-escape.py"),
                              ("/synthetic-absolute.py", "unsafe installed RECORD path: /synthetic-absolute.py")):
            with self.subTest(member=name):
                try:
                    record.write_bytes(original + (name + ",,\n").encode("utf-8"))
                    with self.assertRaisesRegex(AssertionError, re.escape(message)):
                        verify_install(ROOT, self.installation)
                finally:
                    record.write_bytes(original)
        external = self.site / "extra.py"
        try:
            external.write_bytes(b"# synthetic recorded outside-package module\n")
            buffer = io.StringIO()
            digest = base64.urlsafe_b64encode(hashlib.sha256(external.read_bytes()).digest()).rstrip(b"=").decode("ascii")
            csv.writer(buffer).writerow(("extra.py", "sha256=" + digest, str(external.stat().st_size)))
            record.write_bytes(original + buffer.getvalue().encode("utf-8"))
            with self.assertRaisesRegex(AssertionError, "unexpected installed file: .*extra.py"):
                verify_install(ROOT, self.installation)
        finally:
            external.unlink()
            record.write_bytes(original)
        target = self.console.with_name("synthetic-extra" + self.console.suffix)
        try:
            target.write_bytes(b"synthetic unrecorded script")
            with self.assertRaisesRegex(AssertionError, "unexpected installed file: .*synthetic-extra"):
                verify_install(ROOT, self.installation)
        finally:
            target.unlink()
        launcher = self.console.read_bytes()
        try:
            self.console.write_bytes(launcher + b"synthetic damage")
            with self.assertRaisesRegex(AssertionError, "installed bytes differ: .*xhh"):
                verify_install(ROOT, self.installation)
        finally:
            self.console.write_bytes(launcher)

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
        installation = install_wheel(wheel, work)
        self.assertEqual(verify_install(source, installation)["files"], len(source_manifest(ROOT)) + 1)
        with self.assertRaisesRegex(AssertionError, "wheel file set"):
            verify_wheel(ROOT, wheel)


if __name__ == "__main__":
    unittest.main()
