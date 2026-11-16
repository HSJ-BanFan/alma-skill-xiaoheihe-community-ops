"""Structural checks for the packaged skill documentation."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

FENCE = re.compile(r"```.*?```", re.S)
INLINE_CODE = re.compile(r"`[^`\n]*`")
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")


def markdown_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.md")
                  if ".git" not in path.relative_to(root).parts and ".audit" not in path.relative_to(root).parts)


def visible_text(text: str) -> str:
    return INLINE_CODE.sub("", FENCE.sub("", text))


def local_link_targets(text: str) -> list[str]:
    targets = []
    for raw in LINK.findall(visible_text(text)):
        parts = urlsplit(raw)
        if parts.scheme or raw.startswith(("#", "//", "<")):
            continue
        target = unquote(raw.split("#", 1)[0])
        if target:
            targets.append(target)
    return targets


def frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    values = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    return values


def check(root: Path) -> dict:
    root = root.resolve()
    broken = []
    for document in markdown_files(root):
        for target in local_link_targets(document.read_text(encoding="utf-8")):
            resolved = (document.parent / target).resolve()
            if not resolved.exists():
                broken.append({"file": document.relative_to(root).as_posix(), "target": target})
    skill = root / "SKILL.md"
    metadata = frontmatter(skill) if skill.is_file() else {}
    missing_frontmatter = [name for name in ("name", "description") if not metadata.get(name)]
    linked_from_skill = set()
    if skill.is_file():
        linked_from_skill = {unquote(target) for target in
                             local_link_targets(skill.read_text(encoding="utf-8"))}
    references = sorted((root / "references").glob("*.md")) if (root / "references").is_dir() else []
    unlinked_references = [path.relative_to(root).as_posix() for path in references
                           if path.relative_to(root).as_posix() not in linked_from_skill]
    return {"documents": len(markdown_files(root)), "broken_links": broken,
            "missing_frontmatter": missing_frontmatter,
            "unlinked_references": unlinked_references,
            "ok": not broken and not missing_frontmatter and not unlinked_references}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]) if argv else Path(__file__).resolve().parents[1]
    report = check(root)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
