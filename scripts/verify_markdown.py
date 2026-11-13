#!/usr/bin/env python3
"""Check bounded Markdown files for broken relative links and local anchors."""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

_MAX_FILES = 2_000
_MAX_ENTRIES = 20_000
_MAX_FILE_BYTES = 1_048_576
_MAX_TOTAL_BYTES = 16 * 1_048_576
_LINK = re.compile(r"!?\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+[^)]*)?\s*\)")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_SETEXT = re.compile(r"^\s*(?:=+|-+)\s*$")
_HTML_ANCHOR = re.compile(r"\b(?:id|name)=[\"']([^\"']+)[\"']", re.IGNORECASE)


def _slug(text: str) -> str:
    text = text.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
    text = re.sub(r"\s", "-", text)
    return "".join(char for char in text if char == "-" or unicodedata.category(char)[0] in "LMN").strip("-")


def _strip_code(text: str) -> str:
    lines: list[str] = []
    in_fence = False
    fence = ""
    for line in text.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)[0]
            if not in_fence:
                in_fence, fence = True, marker
            elif marker == fence:
                in_fence = False
            continue
        if not in_fence:
            lines.append(re.sub(r"`+[^`]*`+", "", line))
    return "\n".join(lines)


def _anchors(path: Path, root: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return set()
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    lines = _strip_code(text).splitlines()
    for index, line in enumerate(lines):
        heading = _HEADING.match(line)
        heading_text = heading.group(1) if heading else None
        if heading_text is None and index + 1 < len(lines) and line.strip() and _SETEXT.match(lines[index + 1]):
            heading_text = line.strip()
        if heading_text is not None:
            base = _slug(re.sub(r"[`*_~]", "", heading_text))
            count = counts.get(base, 0)
            counts[base] = count + 1
            anchors.add(base if count == 0 else f"{base}-{count}")
        anchors.update(_HTML_ANCHOR.findall(line))
    return anchors


def check(root: Path) -> list[str]:
    root = root.resolve(strict=True)
    files: list[Path] = []
    total_bytes = 0
    paths: list[Path] = []
    for path in root.rglob("*"):
        if len(paths) >= _MAX_ENTRIES:
            return [f"Workspace entry count exceeds {_MAX_ENTRIES}"]
        paths.append(path)
    paths.sort()
    for path in paths:
        if path.suffix.lower() != ".md" or path.is_symlink() or not path.is_file():
            continue
        if len(files) >= _MAX_FILES:
            return [f"Markdown file count exceeds {_MAX_FILES}"]
        try:
            size = path.stat().st_size
        except OSError:
            return [f"Cannot inspect Markdown file: {path.relative_to(root).as_posix()}"]
        if size > _MAX_FILE_BYTES:
            return [f"Markdown file exceeds {_MAX_FILE_BYTES} bytes: {path.relative_to(root).as_posix()}"]
        total_bytes += size
        if total_bytes > _MAX_TOTAL_BYTES:
            return [f"Markdown content exceeds {_MAX_TOTAL_BYTES} bytes"]
        files.append(path)

    anchors_by_file = {path: _anchors(path, root) for path in files}
    errors: list[str] = []
    for source in files:
        try:
            content = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            errors.append(f"{source.relative_to(root).as_posix()}: cannot read UTF-8 Markdown")
            continue
        for line_number, line in enumerate(_strip_code(content).splitlines(), start=1):
            for match in _LINK.finditer(line):
                target = match.group(1) or match.group(2) or ""
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc or target.startswith("//"):
                    continue
                relative = unquote(parsed.path)
                if not relative:
                    destination = source
                else:
                    destination = (source.parent / relative).resolve()
                try:
                    destination.relative_to(root)
                except ValueError:
                    errors.append(f"{source.relative_to(root).as_posix()}:{line_number}: link escapes workspace: {target}")
                    continue
                if destination.is_dir():
                    destination = destination / "index.md"
                if not destination.is_file():
                    errors.append(f"{source.relative_to(root).as_posix()}:{line_number}: missing target: {target}")
                    continue
                fragment = unquote(parsed.fragment)
                if fragment and destination.suffix.lower() == ".md" and destination in anchors_by_file:
                    if fragment not in anchors_by_file[destination]:
                        errors.append(f"{source.relative_to(root).as_posix()}:{line_number}: missing anchor: {target}")
    return errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: verify_markdown.py WORKSPACE", file=sys.stderr)
        return 2
    try:
        errors = check(Path(args[0]))
    except (OSError, ValueError) as exc:
        print(f"documentation check failed: {exc}", file=sys.stderr)
        return 2
    if errors:
        print("Markdown integrity check failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print("Markdown relative links and anchors are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
