"""Shared, fail-closed dependency profile and wheelhouse validation."""

from __future__ import annotations

import hashlib
import io
import json
import platform
import re
import stat
import sys
import sysconfig
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

_MAX_LOCK_BYTES = 1 * 1024 * 1024
_MAX_PYPROJECT_BYTES = 1 * 1024 * 1024
_MAX_REQUIREMENTS_BYTES = 1 * 1024 * 1024
_MAX_PACKAGES = 500
_MAX_WHEELS = 500
_MAX_WHEEL_BYTES = 128 * 1024 * 1024
_MAX_WHEELHOUSE_BYTES = 384 * 1024 * 1024
_MAX_UNPACKED_BYTES = 512 * 1024 * 1024
_MAX_WHEEL_FILES = 50_000
_MAX_BUNDLE_BYTES = 400 * 1024 * 1024
_REGISTRY = "https://pypi.org/simple"
_UV_VERSION = "0.13.7"
_PIP_VERSION = "26.3"
_ALLOWED_ARTIFACT_HOSTS = {"files.pythonhosted.org"}
_WHEEL_NAME = re.compile(r"^[A-Za-z0-9_.+-]+\.whl$")
_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)==(?P<version>[A-Za-z0-9][A-Za-z0-9_.+!-]*)"
    r"(?P<extras>\[[A-Za-z0-9_,.-]+\])?(?:\s*;\s*[A-Za-z0-9_.'<>=! ()-]+)?$"
)
_PYTHON_DEPENDENCY_MANIFESTS = {
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "pdm.lock",
}
_OTHER_LOCKFILES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    "go.sum",
}

class DependencyBlockedError(ValueError):
    """The project dependencies are unsupported or cannot be installed safely."""

@dataclass(frozen=True)
class DependencyInputs:
    pyproject: bytes
    lock: bytes

    @property
    def lock_sha256(self) -> str:
        return hashlib.sha256(self.lock).hexdigest()

@dataclass(frozen=True)
class DependencyEvidence:
    lock_sha256: str
    wheelhouse_sha256: str
    package_count: int
    wheelhouse_bytes: int
    runtime_fingerprint: str = ""
    uv_version: str = _UV_VERSION
    pip_version: str = _PIP_VERSION

def runtime_fingerprint() -> str:
    identity = {
        "python": list(sys.version_info[:3]),
        "implementation": sys.implementation.name,
        "soabi": sysconfig.get_config_var("SOABI"),
        "platform": sysconfig.get_platform(),
        "machine": platform.machine(),
        "libc": list(platform.libc_ver()),
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()

def _read_regular_file(path: Path, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink() or metadata.st_size > maximum:
        raise DependencyBlockedError("A dependency input is not a bounded regular file")
    with path.open("rb") as stream:
        content = stream.read(maximum + 1)
    if len(content) > maximum:
        raise DependencyBlockedError("A dependency input exceeds the configured size limit")
    return content


def _declares_project_dependencies(pyproject: dict[str, object]) -> bool:
    project = pyproject.get("project")
    if isinstance(project, dict) and any(
        bool(project.get(key)) for key in ("dependencies", "optional-dependencies")
    ):
        return True
    groups = pyproject.get("dependency-groups")
    if isinstance(groups, dict) and any(bool(value) for value in groups.values()):
        return True
    tool = pyproject.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict) and isinstance(poetry.get("dependencies"), dict):
            dependencies = poetry["dependencies"]
            if any(name.casefold() != "python" for name in dependencies):
                return True
    return False


def _has_content_dependency_file(path: Path) -> bool:
    try:
        content = _read_regular_file(path, _MAX_REQUIREMENTS_BYTES).decode("utf-8")
    except FileNotFoundError:
        return False
    except (UnicodeDecodeError, DependencyBlockedError):
        return True
    return any(line.strip() and not line.lstrip().startswith("#") for line in content.splitlines())


def discover_dependency_inputs(workspace: Path, *, required: bool) -> DependencyInputs | None:
    """Return a validated root uv.lock pair, or fail closed for declared unsupported deps."""
    lock_path = workspace / "uv.lock"
    project_path = workspace / "pyproject.toml"
    if lock_path.exists() or lock_path.is_symlink():
        if not project_path.exists():
            raise DependencyBlockedError("uv.lock requires a root pyproject.toml")
        try:
            lock = _read_regular_file(lock_path, _MAX_LOCK_BYTES)
            pyproject_bytes = _read_regular_file(project_path, _MAX_PYPROJECT_BYTES)
            pyproject = tomllib.loads(pyproject_bytes.decode("utf-8"))
            lock_data = tomllib.loads(lock.decode("utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise DependencyBlockedError("uv.lock or pyproject.toml is invalid") from exc
        validate_uv_project(pyproject, lock_data)
        return DependencyInputs(pyproject=pyproject_bytes, lock=lock)

    try:
        if project_path.exists() or project_path.is_symlink():
            project_bytes = _read_regular_file(project_path, _MAX_PYPROJECT_BYTES)
            project = tomllib.loads(project_bytes.decode("utf-8"))
            if _declares_project_dependencies(project):
                raise DependencyBlockedError("Project dependencies require a supported root uv.lock")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise DependencyBlockedError("pyproject.toml is invalid") from exc

    for name in _PYTHON_DEPENDENCY_MANIFESTS:
        if _has_content_dependency_file(workspace / name):
            raise DependencyBlockedError("This Python dependency manifest is not supported; use uv.lock")
    for name in _OTHER_LOCKFILES:
        if (workspace / name).exists() or (workspace / name).is_symlink():
            raise DependencyBlockedError("This dependency ecosystem is not supported by this verification profile")
    if required:
        raise DependencyBlockedError("Dependency installation requires a supported root uv.lock")
    return None


def validate_uv_project(project: dict[str, object], lock: dict[str, object]) -> None:
    if lock.get("version") != 1:
        raise DependencyBlockedError("Only uv.lock schema version 1 is supported")
    tool = project.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    if isinstance(uv, dict):
        if uv.get("sources") or uv.get("index") or uv.get("default-index"):
            raise DependencyBlockedError("Custom package indexes and dependency sources are unsupported")
        if uv.get("workspace"):
            raise DependencyBlockedError("uv workspaces are unsupported by this verification profile")
    project_data = project.get("project")
    if not isinstance(project_data, dict):
        raise DependencyBlockedError("pyproject.toml must declare a PEP 621 project")
    if project_data.get("dynamic"):
        raise DependencyBlockedError("Dynamic project metadata is unsupported by the dependency fetcher")
    packages = lock.get("package")
    if not isinstance(packages, list) or len(packages) > _MAX_PACKAGES:
        raise DependencyBlockedError("uv.lock has an invalid or oversized package set")
    for package in packages:
        if not isinstance(package, dict):
            raise DependencyBlockedError("uv.lock contains an invalid package record")
        source = package.get("source", {"registry": _REGISTRY})
        package_name = package.get("name")
        project_name = project_data.get("name")
        if source == {"virtual": "."} and package_name == project_name:
            # The project's own virtual root package is not installed as a wheel.
            continue
        if not isinstance(source, dict) or set(source) != {"registry"}:
            raise DependencyBlockedError("Only registry packages from the public PyPI index are supported")
        if source.get("registry") != _REGISTRY:
            raise DependencyBlockedError("Only the public PyPI index is supported")
        for wheel in package.get("wheels", []):
            if not isinstance(wheel, dict) or not isinstance(wheel.get("url"), str):
                raise DependencyBlockedError("uv.lock contains an invalid wheel source")
            from urllib.parse import urlparse

            parsed = urlparse(wheel["url"])
            if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_ARTIFACT_HOSTS:
                raise DependencyBlockedError("uv.lock contains a wheel outside the trusted artifact host")
            if not isinstance(wheel.get("hash"), str) or not wheel["hash"].startswith("sha256:"):
                raise DependencyBlockedError("Every locked wheel must have a SHA-256 hash")


def parse_exported_requirements(content: bytes) -> list[str]:
    if len(content) > _MAX_REQUIREMENTS_BYTES:
        raise DependencyBlockedError("Exported requirements exceed the configured size limit")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DependencyBlockedError("Exported requirements are not UTF-8") from exc
    logical: list[str] = []
    current = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        current = f"{current} {stripped}" if current else stripped
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        logical.append(current.strip())
        current = ""
    if current:
        raise DependencyBlockedError("Exported requirements contain an incomplete line")
    if len(logical) > _MAX_PACKAGES:
        raise DependencyBlockedError("Project exceeds the package count limit")
    for line in logical:
        hashes = re.findall(r"(?<!\S)--hash=sha256:([a-f0-9]{64})(?=\s|$)", line)
        requirement = re.sub(r"\s+--hash=sha256:[a-f0-9]{64}(?=\s|$)", "", line).strip()
        if not hashes or not _REQUIREMENT.fullmatch(requirement) or "://" in requirement or "@" in requirement:
            raise DependencyBlockedError("Only pinned registry requirements with SHA-256 hashes are supported")
    return logical


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _requirement_identity(line: str) -> tuple[str, str, set[str]]:
    hashes = set(re.findall(r"(?<!\S)--hash=sha256:([a-f0-9]{64})(?=\s|$)", line))
    requirement = re.sub(r"\s+--hash=sha256:[a-f0-9]{64}(?=\s|$)", "", line).strip()
    match = _REQUIREMENT.fullmatch(requirement)
    if not match or not hashes:
        raise DependencyBlockedError("Every requirement must have a pinned version and SHA-256 hash")
    return _canonical_name(match.group("name")), match.group("version").lower(), hashes

def _wheel_identity(filename: str) -> tuple[str, str]:
    if not filename.endswith(".whl"):
        raise DependencyBlockedError("Wheelhouse contains a non-wheel artifact")
    parts = filename[:-4].rsplit("-", 4)
    if len(parts) != 5 or not parts[0] or not parts[1]:
        raise DependencyBlockedError("Wheelhouse contains a malformed wheel filename")
    return _canonical_name(parts[0]), parts[1].lower()


def _validate_wheels_against_requirements(wheels: list[dict[str, object]], requirements: list[str]) -> set[str]:
    expected: dict[str, dict[str, set[str]]] = {}
    for line in requirements:
        name, version, hashes = _requirement_identity(line)
        expected.setdefault(name, {}).setdefault(version, set()).update(hashes)
    seen: set[str] = set()
    for wheel in wheels:
        filename = wheel.get("filename")
        checksum = wheel.get("sha256")
        if not isinstance(filename, str) or not isinstance(checksum, str):
            raise DependencyBlockedError("Wheelhouse manifest contains an invalid record")
        name, version = _wheel_identity(filename)
        if name not in expected or version not in expected[name] or checksum not in expected[name][version]:
            raise DependencyBlockedError("A wheel is not pinned by the exported requirements")
        seen.add(name)
    if seen != set(expected):
        raise DependencyBlockedError("Wheelhouse is missing a locked package wheel")
    return seen


def build_bundle_manifest(
    wheel_dir: Path,
    *,
    lock_sha256: str,
    requirements: bytes,
) -> tuple[dict[str, object], int]:
    required = parse_exported_requirements(requirements)
    wheels: list[dict[str, object]] = []
    total_bytes = 0
    total_unpacked = 0
    files_count = 0
    for path in sorted(wheel_dir.iterdir(), key=lambda item: item.name):
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not _WHEEL_NAME.fullmatch(path.name):
            raise DependencyBlockedError("Wheelhouse contains an unsupported artifact")
        size = metadata.st_size
        if size <= 0 or size > _MAX_WHEEL_BYTES:
            raise DependencyBlockedError("A wheel exceeds the configured size limit")
        total_bytes += size
        if total_bytes > _MAX_WHEELHOUSE_BYTES:
            raise DependencyBlockedError("Wheelhouse exceeds the configured byte limit")
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
        except (OSError, zipfile.BadZipFile) as exc:
            raise DependencyBlockedError("Wheelhouse contains an invalid wheel") from exc
        if not entries:
            raise DependencyBlockedError("Wheelhouse contains an empty wheel")
        for entry in entries:
            member = PurePosixPath(entry.filename)
            if member.is_absolute() or ".." in member.parts:
                raise DependencyBlockedError("Wheel contains an unsafe path")
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise DependencyBlockedError("Wheel contains a symbolic link")
            files_count += 1
            total_unpacked += entry.file_size
            if files_count > _MAX_WHEEL_FILES or total_unpacked > _MAX_UNPACKED_BYTES:
                raise DependencyBlockedError("Wheelhouse exceeds its extracted file or byte limit")
        wheels.append({"filename": path.name, "sha256": _sha256_file(path), "size": size})
        if len(wheels) > _MAX_WHEELS:
            raise DependencyBlockedError("Wheelhouse contains too many wheels")
    _validate_wheels_against_requirements(wheels, required)
    manifest: dict[str, object] = {
        "schema": 1,
        "lock_sha256": lock_sha256,
        "requirements_sha256": hashlib.sha256(requirements).hexdigest(),
        "package_count": len({_requirement_identity(line)[0] for line in required}),
        "wheel_count": len(wheels),
        "wheelhouse_bytes": total_bytes,
        "unpacked_bytes": total_unpacked,
        "runtime_fingerprint": runtime_fingerprint(),
        "wheels": wheels,
    }
    wheelhouse_digest = hashlib.sha256()
    for wheel in wheels:
        wheelhouse_digest.update(str(wheel["filename"]).encode("utf-8") + b"\0" + bytes.fromhex(str(wheel["sha256"])))
    manifest["wheelhouse_sha256"] = wheelhouse_digest.hexdigest()
    return manifest, total_bytes

def create_bundle_archive(
    wheel_dir: Path,
    requirements: bytes,
    manifest: dict[str, object],
    target: BinaryIO,
) -> int:
    size = 0
    with tarfile.open(fileobj=target, mode="w|") as archive:
        def add_bytes(name: str, content: bytes) -> None:
            nonlocal size
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o444
            archive.addfile(info, io.BytesIO(content))
            size += len(content)

        add_bytes("requirements.txt", requirements)
        add_bytes("manifest.json", json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())
        for item in manifest["wheels"]:
            filename = item["filename"]
            path = wheel_dir / filename
            info = tarfile.TarInfo(f"wheels/{filename}")
            info.size = path.stat().st_size
            info.mode = 0o444
            with path.open("rb") as stream:
                archive.addfile(info, stream)
            size += info.size
            if size > _MAX_BUNDLE_BYTES:
                raise DependencyBlockedError("Dependency bundle exceeds its transfer limit")
    target.flush()
    return size


def extract_bundle_archive(source: BinaryIO, destination: Path, expected_lock_sha256: str) -> DependencyEvidence:
    destination.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    total_bytes = 0
    requirements: bytes | None = None
    manifest_bytes: bytes | None = None
    wheel_paths: list[Path] = []
    try:
        archive = tarfile.open(fileobj=source, mode="r|")
        with archive:
            for member in archive:
                name = member.name
                if name in seen or member.issym() or member.islnk() or not member.isfile():
                    raise DependencyBlockedError("Dependency bundle contains an unsupported archive entry")
                seen.add(name)
                if name in {"requirements.txt", "manifest.json"}:
                    if member.size > _MAX_REQUIREMENTS_BYTES:
                        raise DependencyBlockedError("Dependency bundle metadata exceeds its limit")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise DependencyBlockedError("Dependency bundle is truncated")
                    content = stream.read(member.size + 1)
                    if len(content) != member.size:
                        raise DependencyBlockedError("Dependency bundle is truncated")
                    if name == "requirements.txt":
                        requirements = content
                    else:
                        manifest_bytes = content
                elif name.startswith("wheels/") and _WHEEL_NAME.fullmatch(name.removeprefix("wheels/")):
                    total_bytes += member.size
                    if member.size <= 0 or member.size > _MAX_WHEEL_BYTES or total_bytes > _MAX_WHEELHOUSE_BYTES:
                        raise DependencyBlockedError("Dependency bundle wheel exceeds its byte limit")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise DependencyBlockedError("Dependency bundle is truncated")
                    path = destination / name.removeprefix("wheels/")
                    with path.open("xb") as output:
                        remaining = member.size
                        while remaining:
                            chunk = stream.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise DependencyBlockedError("Dependency bundle is truncated")
                            output.write(chunk)
                            remaining -= len(chunk)
                    path.chmod(0o444)
                    wheel_paths.append(path)
                else:
                    raise DependencyBlockedError("Dependency bundle contains an unexpected path")
                if total_bytes > _MAX_BUNDLE_BYTES:
                    raise DependencyBlockedError("Dependency bundle exceeds its transfer limit")
    except (OSError, tarfile.TarError) as exc:
        raise DependencyBlockedError("Dependency bundle is invalid") from exc

    if requirements is None or manifest_bytes is None:
        raise DependencyBlockedError("Dependency bundle is missing required metadata")
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise DependencyBlockedError("Dependency bundle manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != 1 or manifest.get("lock_sha256") != expected_lock_sha256:
        raise DependencyBlockedError("Dependency bundle does not match the locked workspace")
    if manifest.get("runtime_fingerprint") != runtime_fingerprint():
        raise DependencyBlockedError("Fetcher and supervisor Python ABI or platform do not match")
    if manifest.get("toolchain") != {"uv": _UV_VERSION, "pip": _PIP_VERSION}:
        raise DependencyBlockedError("Dependency fetcher toolchain version is unsupported")
    logical_requirements = parse_exported_requirements(requirements)
    listed = manifest.get("wheels")
    if (
        not isinstance(listed, list)
        or len(listed) != len(wheel_paths)
        or manifest.get("wheel_count") != len(wheel_paths)
    ):
        raise DependencyBlockedError("Dependency bundle package or wheel count does not match its lock")
    package_names = _validate_wheels_against_requirements(listed, logical_requirements)
    if manifest.get("package_count") != len(package_names):
        raise DependencyBlockedError("Dependency bundle package count is invalid")
    if hashlib.sha256(requirements).hexdigest() != manifest.get("requirements_sha256"):
        raise DependencyBlockedError("Dependency bundle requirements digest mismatch")
    digest = hashlib.sha256()
    total = 0
    for entry, path in zip(listed, sorted(wheel_paths, key=lambda item: item.name), strict=True):
        if not isinstance(entry, dict) or entry.get("filename") != path.name:
            raise DependencyBlockedError("Dependency bundle wheel list is invalid")
        actual = _sha256_file(path)
        if entry.get("sha256") != actual or entry.get("size") != path.stat().st_size:
            raise DependencyBlockedError("Dependency bundle wheel digest mismatch")
        digest.update(path.name.encode("utf-8") + b"\0" + bytes.fromhex(actual))
        total += path.stat().st_size
    if total != manifest.get("wheelhouse_bytes"):
        raise DependencyBlockedError("Dependency bundle total size mismatch")
    wheelhouse_sha = digest.hexdigest()
    if manifest.get("wheelhouse_sha256") != wheelhouse_sha:
        raise DependencyBlockedError("Dependency bundle wheelhouse digest mismatch")
    return DependencyEvidence(expected_lock_sha256, wheelhouse_sha, len(package_names), total, runtime_fingerprint())
