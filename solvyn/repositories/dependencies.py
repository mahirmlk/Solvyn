"""Bounded, static dependency-name extraction from repository manifests.

Repository manifests are untrusted input. This module deliberately performs no
network access, version resolution, build evaluation, or code execution. It
extracts only direct dependency names from a small, bounded set of files.
"""

from __future__ import annotations

import json
import re
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

MAX_MANIFESTS = 25
MAX_MANIFEST_BYTES = 64_000
MAX_TOTAL_BYTES = 512_000
MAX_DEPENDENCIES_PER_MANIFEST = 200
SUPPORTED_MANIFEST_BASENAMES = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "go.mod",
        "cargo.toml",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "gemfile",
        "composer.json",
    }
)


def is_dependency_manifest_path(path: str) -> bool:
    """Whether a repository path is a supported dependency manifest."""
    basename = PurePosixPath(path.replace("\\", "/")).name.lower()
    return (
        basename in SUPPORTED_MANIFEST_BASENAMES
        or basename.startswith("requirements") and basename.endswith(".txt")
        or basename.endswith(".requirements.txt")
    )

_PYTHON_REQUIREMENT_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[[A-Za-z0-9._,-]+\])?"
    r"(?=\s|===|==|~=|!=|<=|>=|<|>|;|@|$)"
)
_GRADLE_COORDINATE_RE = re.compile(
    r"\b(?:implementation|api|compileOnly|runtimeOnly|testImplementation|testRuntimeOnly|annotationProcessor)"
    r"\s*(?:\(\s*)?['\"]([^:'\"]+):([^:'\"]+):[^'\"]+['\"]"
)
_GRADLE_KOTLIN_RE = re.compile(
    r"\b(?:implementation|api|compileOnly|runtimeOnly|testImplementation|testRuntimeOnly|annotationProcessor)"
    r"\s*\(\s*['\"]([^:'\"]+):([^:'\"]+):[^'\"]+['\"]\s*\)"
)


def parse_dependencies(manifests: Mapping[str, str]) -> dict[str, list[str]]:
    """Return bounded direct dependency names for supported manifest paths.

    At most :data:`MAX_MANIFESTS` input entries are visited, each manifest is
    limited to :data:`MAX_MANIFEST_BYTES`, the aggregate input budget is
    :data:`MAX_TOTAL_BYTES`, and each result contains at most
    :data:`MAX_DEPENDENCIES_PER_MANIFEST` names. Input iteration order is
    normalized by path, so results are deterministic for equivalent mappings.

    Unsupported, malformed, empty, and oversized manifests produce an empty
    list. The original input path is retained as the result key, including for
    nested manifests. Version constraints are intentionally omitted from all
    returned values.
    """
    found: dict[str, list[str]] = {}
    total_bytes = 0
    ordered_paths = sorted(manifests)[:MAX_MANIFESTS]

    for path in ordered_paths:
        raw = manifests[path]
        # Encoding failures are not expected for decoded provider text, but
        # treat unusual strings as unsupported instead of propagating errors.
        try:
            encoded_size = len(raw.encode("utf-8"))
        except UnicodeEncodeError:
            found[path] = []
            continue
        if encoded_size > MAX_MANIFEST_BYTES or total_bytes + encoded_size > MAX_TOTAL_BYTES:
            found[path] = []
            continue
        total_bytes += encoded_size
        basename = PurePosixPath(path.replace("\\", "/")).name.lower()
        try:
            names = _parse_manifest(basename, raw)
        except (ValueError, TypeError, KeyError, tomllib.TOMLDecodeError, ET.ParseError):
            names = []
        found[path] = _bounded_names(names)
    return found


def _parse_manifest(basename: str, raw: str) -> list[str]:
    if basename in {"package.json", "composer.json"}:
        return _parse_json(basename, raw)
    if basename == "pyproject.toml":
        return _parse_pyproject(raw)
    if (basename.startswith("requirements") or basename.endswith(".requirements.txt")) and basename.endswith(".txt"):
        return _parse_requirements(raw)
    if basename == "go.mod":
        return _parse_go_mod(raw)
    if basename == "cargo.toml":
        return _parse_cargo(raw)
    if basename == "pom.xml":
        return _parse_maven(raw)
    if basename in {"build.gradle", "build.gradle.kts"}:
        return _parse_gradle(raw)
    if basename == "gemfile":
        return _parse_gemfile(raw)
    return []


def _parse_json(basename: str, raw: str) -> list[str]:
    document = json.loads(raw)
    if not isinstance(document, dict):
        return []
    sections: tuple[str, ...] = (
        ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
        if basename == "package.json"
        else ("require", "require-dev")
    )
    names: set[str] = set()
    for section in sections:
        dependencies = document.get(section, {})
        if isinstance(dependencies, dict):
            names.update(
                name
                for name in dependencies
                if isinstance(name, str)
                and _valid_name(name)
                and (basename != "composer.json" or name.lower() != "php" and not name.lower().startswith("ext-"))
            )
    return sorted(names)


def _parse_pyproject(raw: str) -> list[str]:
    document = tomllib.loads(raw)
    project = document.get("project", {})
    names: set[str] = set()
    if isinstance(project, dict):
        names.update(_python_requirement_name(item) for item in _as_list(project.get("dependencies")))
        optional = project.get("optional-dependencies", {})
        if isinstance(optional, dict):
            for entries in optional.values():
                names.update(_python_requirement_name(item) for item in _as_list(entries))
        groups = document.get("dependency-groups", {})
        if isinstance(groups, dict):
            for entries in groups.values():
                names.update(_python_requirement_name(item) for item in _as_list(entries))

    # Support Poetry's common static declarations without interpreting plugin
    # or dynamic configuration.
    tool = document.get("tool", {})
    poetry = tool.get("poetry", {}) if isinstance(tool, dict) else {}
    if isinstance(poetry, dict):
        names.update(name for name in poetry.get("dependencies", {}) if isinstance(name, str) and name.lower() != "python")
        names.update(name for name in poetry.get("dev-dependencies", {}) if isinstance(name, str))
        groups = poetry.get("group", {})
        if isinstance(groups, dict):
            for group in groups.values():
                dependencies = group.get("dependencies", {}) if isinstance(group, dict) else {}
                if isinstance(dependencies, dict):
                    names.update(name for name in dependencies if isinstance(name, str))
    return sorted(name for name in names if name)


def _parse_requirements(raw: str) -> list[str]:
    names: set[str] = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        match = _PYTHON_REQUIREMENT_RE.match(stripped)
        if match:
            names.add(match.group(1))
    return sorted(names)


def _parse_go_mod(raw: str) -> list[str]:
    names: set[str] = set()
    in_require_block = False
    for line in raw.splitlines():
        stripped = line.split("//", 1)[0].strip()
        if not stripped:
            continue
        if stripped.startswith("require ("):
            in_require_block = True
            continue
        if in_require_block and stripped == ")":
            in_require_block = False
            continue
        if stripped.startswith("require "):
            parts = stripped[len("require "):].split()
            if parts and _valid_name(parts[0]):
                names.add(parts[0])
            continue
        if in_require_block:
            parts = stripped.split()
            if parts and _valid_name(parts[0]):
                names.add(parts[0])
    return sorted(names)


def _parse_cargo(raw: str) -> list[str]:
    document = tomllib.loads(raw)
    names: set[str] = set()
    for section_name, section in document.items():
        if section_name in {"dependencies", "dev-dependencies", "build-dependencies"} and isinstance(section, dict):
            names.update(name for name in section if isinstance(name, str))
        elif section_name == "target" and isinstance(section, dict):
            for target in section.values():
                if not isinstance(target, dict):
                    continue
                for dependency_type in ("dependencies", "dev-dependencies", "build-dependencies"):
                    dependencies = target.get(dependency_type, {})
                    if isinstance(dependencies, dict):
                        names.update(name for name in dependencies if isinstance(name, str))
    return sorted(names)


def _parse_maven(raw: str) -> list[str]:
    # Entity expansion is unnecessary for dependency names and can amplify
    # untrusted XML input. Reject declarations before handing it to Expat.
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", raw, flags=re.IGNORECASE):
        return []
    root = ET.fromstring(raw)
    names: set[str] = set()
    # Maven XML commonly uses a default namespace, so compare local names.
    for child in root:
        if _local_name(child.tag) != "dependencies":
            continue
        for dependency in child:
            if _local_name(dependency.tag) != "dependency":
                continue
            values = {_local_name(item.tag): (item.text or "").strip() for item in dependency}
            group = values.get("groupId", "")
            artifact = values.get("artifactId", "")
            if group and artifact:
                names.add(f"{group}:{artifact}")
    return sorted(names)


def _parse_gradle(raw: str) -> list[str]:
    names: set[str] = set()
    in_block_comment = False
    for line in raw.splitlines():
        visible: list[str] = []
        index = 0
        while index < len(line):
            if in_block_comment:
                end = line.find("*/", index)
                if end < 0:
                    break
                in_block_comment = False
                index = end + 2
            elif line.startswith("/*", index):
                in_block_comment = True
                index += 2
            elif line.startswith("//", index):
                break
            else:
                visible.append(line[index])
                index += 1
        candidate = "".join(visible)
        for pattern in (_GRADLE_COORDINATE_RE, _GRADLE_KOTLIN_RE):
            names.update(f"{group}:{artifact}" for group, artifact in pattern.findall(candidate))
    return sorted(names)


def _parse_gemfile(raw: str) -> list[str]:
    names: set[str] = set()
    for line in raw.splitlines():
        match = re.match(r"^\s*gem\s+['\"]([^'\"]+)['\"]", line)
        if match and _valid_name(match.group(1)):
            names.add(match.group(1))
    return sorted(names)


def _python_requirement_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    match = _PYTHON_REQUIREMENT_RE.match(value)
    return match.group(1) if match else ""


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _valid_name(name: str) -> bool:
    return bool(name) and len(name) <= 300 and "\x00" not in name


def _bounded_names(names: list[str]) -> list[str]:
    return sorted({name for name in names if isinstance(name, str) and _valid_name(name)})[:MAX_DEPENDENCIES_PER_MANIFEST]
