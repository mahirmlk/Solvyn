"""Bounded parser and validator for root npm package-lock v2/v3 inputs."""
from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import os
import re
import shlex
import ssl
import stat
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast
from urllib.parse import urlsplit

from solvyn.sandbox.dependencies import DependencyBlockedError, DependencyEvidence, _read_regular_file

MAX_MANIFEST_BYTES = 512 * 1024
MAX_LOCK_BYTES = 4 * 1024 * 1024
MAX_PACKAGES = 500
MAX_TARBALL_BYTES = 32 * 1024 * 1024
MAX_TOTAL_TARBALL_BYTES = 256 * 1024 * 1024
MAX_UNPACKED_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_FILES = 50_000
REGISTRY_ORIGIN = "registry.npmjs.org"
MAX_BUNDLE_BYTES = 400 * 1024 * 1024
_SRI = re.compile(r"^sha512-([A-Za-z0-9+/]{86}==)$")


@dataclass(frozen=True)
class NpmPackage:
    lock_path: str
    resolved: str
    integrity: str
    name: str
    version: str


@dataclass(frozen=True)
class NpmDependencyInputs:
    manifest: bytes
    lock: bytes
    packages: tuple[NpmPackage, ...]

    @property
    def lock_sha256(self) -> str:
        return hashlib.sha256(self.lock).hexdigest()


def _pairs_no_duplicates(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise DependencyBlockedError("npm manifest or lock contains duplicate JSON keys")
        result[key] = value
    return result


def _json_object(content: bytes, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(content, object_pairs_hook=_pairs_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DependencyBlockedError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise DependencyBlockedError(f"{label} must contain a JSON object")
    return value


def _resolve_locked_dependency(parent_path: str, name: str, package_paths: set[str]) -> str | None:
    dependency_parts = name.split("/")
    if not name or any(part in {"", ".", ".."} for part in dependency_parts):
        raise DependencyBlockedError("package-lock dependency name is malformed")
    if not parent_path:
        candidate = "node_modules/" + name
        return candidate if candidate in package_paths else None
    parts = parent_path.split("/")
    module_indices = [index for index, part in enumerate(parts) if part == "node_modules"]
    for index in reversed(module_indices):
        candidate = "/".join([*parts[:index + 1], *dependency_parts])
        if candidate in package_paths:
            return candidate
    return None


def _validate_resolved(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise DependencyBlockedError("package-lock contains an invalid tarball URL")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise DependencyBlockedError("package-lock contains an invalid tarball URL") from exc
    try:
        port = parsed.port
    except ValueError as exc:
        raise DependencyBlockedError("package-lock contains an invalid tarball URL") from exc
    try:
        ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        pass
    else:
        raise DependencyBlockedError("package-lock tarballs must use the public npm registry hostname")
    if (
        parsed.scheme != "https"
        or parsed.hostname != REGISTRY_ORIGIN
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc != REGISTRY_ORIGIN
        or not parsed.path.startswith("/")
        or "\\" in parsed.path
        or "%" in parsed.path
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise DependencyBlockedError("Only direct HTTPS tarballs from registry.npmjs.org are supported")
    return value


def validate_npm_pair(manifest_bytes: bytes, lock_bytes: bytes) -> NpmDependencyInputs:
    if not manifest_bytes or len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise DependencyBlockedError("package.json exceeds the configured size limit")
    if not lock_bytes or len(lock_bytes) > MAX_LOCK_BYTES:
        raise DependencyBlockedError("package-lock.json exceeds the configured size limit")
    manifest = _json_object(manifest_bytes, label="package.json")
    lock = _json_object(lock_bytes, label="package-lock.json")
    if lock.get("lockfileVersion") not in (2, 3) or type(lock.get("lockfileVersion")) is not int:
        raise DependencyBlockedError("Only npm package-lock versions 2 and 3 are supported")
    if "workspaces" in manifest:
        raise DependencyBlockedError("npm workspaces are unsupported")
    if any(key in manifest for key in ("overrides", "bundleDependencies", "bundledDependencies")):
        raise DependencyBlockedError("npm overrides and bundled dependencies are unsupported")
    for field in ("name", "version"):
        if field in lock and lock.get(field) != manifest.get(field):
            raise DependencyBlockedError(f"package.json and package-lock disagree on {field}")
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        declarations = manifest.get(field, {})
        if not isinstance(declarations, dict) or any(
            not isinstance(name, str) or not isinstance(version, str) for name, version in declarations.items()
        ):
            raise DependencyBlockedError(f"package.json {field} must be a package-to-version map")

    locked_packages = lock.get("packages")
    if not isinstance(locked_packages, dict) or "" not in locked_packages or len(locked_packages) > MAX_PACKAGES + 1:
        raise DependencyBlockedError("package-lock has an invalid or oversized package set")
    root = locked_packages[""]
    if not isinstance(root, dict):
        raise DependencyBlockedError("package-lock root package record is invalid")
    for field in ("name", "version", "dependencies", "devDependencies", "optionalDependencies"):
        if field in manifest and root.get(field) != manifest[field]:
            raise DependencyBlockedError(f"package.json and package-lock root disagree on {field}")
        if field not in manifest and field in root:
            raise DependencyBlockedError(f"package-lock has unpaired root {field}")

    packages: list[NpmPackage] = []
    package_records: dict[str, dict[str, object]] = {}
    for lock_path, raw in locked_packages.items():
        if lock_path == "":
            continue
        if not isinstance(lock_path, str) or not lock_path.startswith("node_modules/"):
            raise DependencyBlockedError("Only root node_modules package locations are supported")
        path_parts = lock_path.split("/")
        if any(part in {"", ".", ".."} for part in path_parts) or "\\" in lock_path:
            raise DependencyBlockedError("package-lock contains an unsafe package path")
        if not isinstance(raw, dict) or raw.get("link") or raw.get("inBundle"):
            raise DependencyBlockedError("Local links and bundled packages are unsupported")
        if raw.get("hasInstallScript") is True:
            raise DependencyBlockedError("Packages requiring lifecycle scripts are unsupported")
        if raw.get("dev") is True and raw.get("optional") is True:
            # These fields are valid together; retain them. This branch deliberately
            # avoids interpreting lock metadata as install behavior.
            pass
        name, version = raw.get("name"), raw.get("version")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise DependencyBlockedError("package-lock package name or version is missing")
        package_parts = lock_path.split("/")
        last_modules = len(package_parts) - 1 - package_parts[::-1].index("node_modules")
        if last_modules + 1 >= len(package_parts):
            raise DependencyBlockedError("package-lock package path is malformed")
        if package_parts[last_modules + 1].startswith("@"):
            if last_modules + 2 >= len(package_parts):
                raise DependencyBlockedError("package-lock scoped package path is malformed")
            path_name = "/".join(package_parts[last_modules + 1:last_modules + 3])
            if last_modules + 3 != len(package_parts):
                raise DependencyBlockedError("package-lock package path has unexpected segments")
        else:
            path_name = package_parts[-1]
            if last_modules + 2 != len(package_parts):
                raise DependencyBlockedError("package-lock package path has unexpected segments")
        if name != path_name:
            raise DependencyBlockedError("package-lock package name does not match its install path")
        for field in ("dependencies", "optionalDependencies", "peerDependencies"):
            declarations = raw.get(field, {})
            if not isinstance(declarations, dict) or any(
                not isinstance(dep_name, str) or not isinstance(dep_version, str)
                for dep_name, dep_version in declarations.items()
            ):
                raise DependencyBlockedError("package-lock dependency metadata is malformed")
        for field in ("dev", "optional", "devOptional", "hasInstallScript"):
            if field in raw and type(raw[field]) is not bool:
                raise DependencyBlockedError("package-lock install metadata is malformed")
        resolved = _validate_resolved(raw.get("resolved"))
        integrity_value = raw.get("integrity")
        if not isinstance(integrity_value, str):
            raise DependencyBlockedError("Every npm package must have exactly one SHA-512 integrity value")
        integrity = integrity_value
        match = _SRI.fullmatch(integrity)
        if not match:
            raise DependencyBlockedError("Every npm package must have exactly one SHA-512 integrity value")
        try:
            decoded = base64.b64decode(match.group(1), validate=True)
        except ValueError as exc:
            raise DependencyBlockedError("package-lock contains invalid SHA-512 integrity") from exc
        if len(decoded) != 64:
            raise DependencyBlockedError("package-lock contains invalid SHA-512 integrity")
        packages.append(NpmPackage(lock_path, resolved, integrity, name, version))
        package_records[lock_path] = raw
    if len(packages) > MAX_PACKAGES:
        raise DependencyBlockedError("package-lock exceeds the package count limit")
    if lock.get("lockfileVersion") == 2:
        legacy = lock.get("dependencies")
        if not isinstance(legacy, dict):
            raise DependencyBlockedError("Version 2 package-lock requires the legacy dependency map")
        legacy_seen: set[str] = set()
        def walk_legacy(dependencies: object, parent: str = "") -> None:
            if not isinstance(dependencies, dict):
                raise DependencyBlockedError("Version 2 package-lock legacy dependency map is invalid")
            for dep_name, entry in dependencies.items():
                if not isinstance(dep_name, str) or not isinstance(entry, dict):
                    raise DependencyBlockedError("Version 2 package-lock legacy dependency map is invalid")
                package_path = f"{parent}/node_modules/{dep_name}" if parent else f"node_modules/{dep_name}"
                package = locked_packages.get(package_path)
                if not isinstance(package, dict) or entry.get("version") != package.get("version"):
                    raise DependencyBlockedError("Version 2 legacy dependency map disagrees with packages")
                legacy_seen.add(package_path)
                for field in ("resolved", "integrity"):
                    if field in entry and entry.get(field) != package.get(field):
                        raise DependencyBlockedError("Version 2 legacy dependency map disagrees with packages")
                walk_legacy(entry.get("dependencies", {}), package_path)
        walk_legacy(legacy)
        if legacy_seen != set(locked_packages) - {""}:
            raise DependencyBlockedError("Version 2 legacy dependency map does not cover the packages table")
    elif "dependencies" in lock:
        raise DependencyBlockedError("Version 3 package-lock must not include a legacy dependency map")

    package_paths = set(package_records)
    reachable: set[str] = set()
    pending: list[str] = []
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        declarations = manifest.get(field, {})
        if not isinstance(declarations, dict):
            raise DependencyBlockedError("package.json root dependency map is malformed")
        for name in declarations:
            if not isinstance(name, str):
                raise DependencyBlockedError("package.json dependency name is malformed")
            path = _resolve_locked_dependency("", name, package_paths)
            if path is None:
                raise DependencyBlockedError("package-lock omits a declared root dependency")
            pending.append(path)
    while pending:
        path = pending.pop()
        if path in reachable:
            continue
        reachable.add(path)
        package_record = package_records[path]
        for field in ("dependencies", "optionalDependencies", "peerDependencies"):
            dependencies = package_record.get(field, {})
            assert isinstance(dependencies, dict)
            peer_meta = package_record.get("peerDependenciesMeta", {})
            if not isinstance(peer_meta, dict):
                raise DependencyBlockedError("package-lock peer dependency metadata is malformed")
            for name in dependencies:
                target = _resolve_locked_dependency(path, name, package_paths)
                optional_peer = (
                    field == "peerDependencies"
                    and isinstance(peer_meta.get(name), dict)
                    and peer_meta[name].get("optional") is True
                )
                if target is None:
                    if field == "optionalDependencies" or optional_peer:
                        continue
                    raise DependencyBlockedError("package-lock omits a transitive dependency")
                pending.append(target)
    if reachable != package_paths:
        raise DependencyBlockedError("package-lock contains packages outside the declared dependency graph")
    return NpmDependencyInputs(manifest_bytes, lock_bytes, tuple(packages))


def discover_npm_inputs(workspace: Path) -> NpmDependencyInputs | None:
    manifest_path = workspace / "package.json"
    lock_path = workspace / "package-lock.json"
    if not manifest_path.exists() and not lock_path.exists() and not manifest_path.is_symlink() and not lock_path.is_symlink():
        return None
    if not manifest_path.exists() or not lock_path.exists():
        raise DependencyBlockedError("Root package.json and package-lock.json are required as a pair")
    for path in workspace.rglob("*"):
        if path.is_symlink():
            raise DependencyBlockedError("Symlinks are unsupported in npm dependency workspaces")
        if path.is_file() and path.name.casefold() in {
            "pyproject.toml", "uv.lock", "requirements.txt", "requirements-dev.txt",
            "requirements-test.txt", "setup.cfg", "setup.py", "pipfile", "pipfile.lock",
            "poetry.lock", "pdm.lock", "conda-lock.yml", "conda-lock.yaml", ".npmrc",
        }:
            raise DependencyBlockedError("Mixed Python projects and custom npm configuration are unsupported")
        if path.is_file() and path.name.casefold() in {"package.json", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock"}:
            relative = path.relative_to(workspace)
            if relative not in {Path("package.json"), Path("package-lock.json")}:
                raise DependencyBlockedError("Nested projects and other JavaScript package managers are unsupported")
    validated = validate_npm_pair(
        _read_regular_file(manifest_path, MAX_MANIFEST_BYTES),
        _read_regular_file(lock_path, MAX_LOCK_BYTES),
    )
    return validated if validated.packages else None


def _verify_integrity(content: bytes, sri: str) -> None:
    match = _SRI.fullmatch(sri)
    if match is None:
        raise DependencyBlockedError("npm tarball integrity value is invalid")
    expected = base64.b64decode(match.group(1), validate=True)
    if hashlib.sha512(content).digest() != expected:
        raise DependencyBlockedError("An npm tarball does not match its lockfile SHA-512 integrity")


def _safe_package_tarball(content: bytes, package_root: Path, counters: list[int]) -> None:
    try:
        archive = tarfile.open(fileobj=io.BytesIO(content), mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise DependencyBlockedError("An npm package tarball is invalid") from exc
    seen: set[str] = set()
    with archive:
        for member in archive:
            name = member.name
            if name.startswith("./"):
                name = name[2:]
            if not name.startswith("package/"):
                raise DependencyBlockedError("npm tarball contains a path outside its package root")
            relative = name.removeprefix("package/")
            if not relative:
                continue
            parts = relative.split("/")
            if any(part in {"", ".", ".."} for part in parts) or "\\" in relative or relative.startswith("/"):
                raise DependencyBlockedError("npm tarball contains an unsafe path")
            if relative in seen:
                raise DependencyBlockedError("npm tarball contains duplicate paths")
            seen.add(relative)
            target = package_root.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile() or member.size < 0:
                raise DependencyBlockedError("npm tarball contains a link or special filesystem entry")
            counters[0] += 1
            counters[1] += member.size
            if counters[0] > MAX_ARCHIVE_FILES or counters[1] > MAX_UNPACKED_BYTES:
                raise DependencyBlockedError("npm packages exceed the extracted file or byte limit")
            if target.exists() or target.is_symlink():
                raise DependencyBlockedError("npm tarballs contain colliding package paths")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise DependencyBlockedError("npm tarball is truncated")
            with source, target.open("xb") as output:
                remaining = member.size
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise DependencyBlockedError("npm tarball is truncated")
                    output.write(chunk)
                    remaining -= len(chunk)
                target.chmod(0o444)


def _verify_package_metadata(package_root: Path, expected: NpmPackage) -> None:
    metadata_path = package_root / "package.json"
    if not metadata_path.is_file() or metadata_path.is_symlink() or metadata_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise DependencyBlockedError("npm tarball is missing bounded package metadata")
    metadata = _json_object(metadata_path.read_bytes(), label="npm package metadata")
    if metadata.get("name") != expected.name or metadata.get("version") != expected.version:
        raise DependencyBlockedError("npm tarball package identity does not match package-lock")


def _write_root_bin_shims(inputs: NpmDependencyInputs, node_modules: Path) -> None:
    manifest = _json_object(inputs.manifest, label="package.json")
    direct: set[str] = set()
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        declarations = manifest.get(field, {})
        if isinstance(declarations, dict):
            direct.update(f"node_modules/{name}" for name in declarations if isinstance(name, str))
    seen_bins: set[str] = set()
    for package in inputs.packages:
        if package.lock_path not in direct:
            continue
        package_dir = node_modules.joinpath(*package.lock_path.split("/")[1:])
        metadata_path = package_dir / "package.json"
        if not metadata_path.is_file():
            continue
        metadata = _json_object(metadata_path.read_bytes(), label="npm package metadata")
        raw_bin = metadata.get("bin")
        if isinstance(raw_bin, str):
            default_name = package.name.rsplit("/", 1)[-1]
            bin_items: dict[str, object] = {default_name: raw_bin}
        elif isinstance(raw_bin, dict):
            bin_items = raw_bin
        elif raw_bin is None:
            continue
        else:
            raise DependencyBlockedError("An npm package has an invalid bin declaration")
        bin_dir = node_modules / ".bin"
        for name, target in bin_items.items():
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,128}", name)
                    or name in {".", ".."} or name in seen_bins or not isinstance(target, str)):
                raise DependencyBlockedError("npm package bin declarations are ambiguous or invalid")
            seen_bins.add(name)
            target_path = (package_dir / target).resolve(strict=False)
            if not target_path.is_relative_to(package_dir.resolve()) or not target_path.is_file():
                raise DependencyBlockedError("npm package bin target is missing or unsafe")
            shim = bin_dir / name
            bin_dir.mkdir(parents=True, exist_ok=True)
            shim.write_text(
                "#!/bin/sh\nexec node " + shlex.quote("/workspace/node_modules/" + Path(os.path.relpath(target_path, node_modules)).as_posix()) + " \"$@\"\n",
                encoding="utf-8",
            )
            shim.chmod(0o555)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: object, response: object, code: int, message: str, headers: object, new_url: str) -> None:
        return None


def fetch_npm_bundle(inputs: NpmDependencyInputs, *, proxy: str) -> tuple[BinaryIO, dict[str, object], tempfile.TemporaryDirectory[str]]:
    """Fetch only lock-enumerated archives, verify SRI, and create a bounded tar bundle."""
    temporary = tempfile.TemporaryDirectory(prefix="solvyn-npm-deps-")
    root = Path(temporary.name)
    node_modules = root / "node_modules"
    node_modules.mkdir(mode=0o700)
    counters = [0, 0]
    total_transfer = 0
    deadline = time.monotonic() + 180
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            _NoRedirect(),
        )
        for package in inputs.packages:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise DependencyBlockedError("npm dependency retrieval exceeded its overall time limit")
            request = urllib.request.Request(package.resolved, headers={"Accept-Encoding": "identity"})
            try:
                response = opener.open(request, timeout=min(15, remaining_time))
            except urllib.error.HTTPError as exc:
                raise DependencyBlockedError("Locked npm tarball retrieval was refused or redirected") from exc
            with response:
                if response.status != 200 or response.geturl() != package.resolved:
                    raise DependencyBlockedError("Locked npm tarball retrieval was refused or redirected")
                length = response.headers.get("Content-Length")
                if length is None or not length.isdecimal() or not 1 <= int(length) <= MAX_TARBALL_BYTES:
                    raise DependencyBlockedError("An npm tarball has an invalid or excessive size")
                if total_transfer + int(length) > MAX_TOTAL_TARBALL_BYTES:
                    raise DependencyBlockedError("npm tarballs exceed their aggregate size limit")
                content_buffer = bytearray()
                while len(content_buffer) <= int(length):
                    if time.monotonic() >= deadline:
                        raise DependencyBlockedError("npm dependency retrieval exceeded its overall time limit")
                    chunk = response.read(min(64 * 1024, int(length) + 1 - len(content_buffer)))
                    if not chunk:
                        break
                    content_buffer.extend(chunk)
                    if len(content_buffer) > int(length):
                        break
                content = bytes(content_buffer)
                if len(content) != int(length):
                    raise DependencyBlockedError("An npm tarball transfer was truncated or oversized")
            total_transfer += len(content)
            if total_transfer > MAX_TOTAL_TARBALL_BYTES:
                raise DependencyBlockedError("npm tarballs exceed their aggregate size limit")
            _verify_integrity(content, package.integrity)
            destination = node_modules.joinpath(*package.lock_path.split("/")[1:])
            destination.mkdir(parents=True, exist_ok=True)
            _safe_package_tarball(content, destination, counters)
            _verify_package_metadata(destination, package)
        _write_root_bin_shims(inputs, node_modules)

        archive_file = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b", dir=temporary.name)
        total_archive_bytes = 0
        with tarfile.open(fileobj=archive_file, mode="w|", format=tarfile.PAX_FORMAT) as archive:
            for path in sorted(node_modules.rglob("*"), key=lambda item: item.as_posix()):
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise DependencyBlockedError("Staged npm package contains an unsupported filesystem entry")
                name = path.relative_to(root).as_posix()
                relative = Path(name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise DependencyBlockedError("Staged npm package has an unsafe path")
                member = tarfile.TarInfo(name)
                member.type = tarfile.REGTYPE
                member.mode = 0o555 if name.startswith("node_modules/.bin/") else 0o444
                member.size = info.st_size
                with path.open("rb") as source:
                    archive.addfile(member, source)
                total_archive_bytes += info.st_size
                if total_archive_bytes > MAX_BUNDLE_BYTES:
                    raise DependencyBlockedError("npm bundle exceeds its transfer limit")
        archive_file.seek(0)
        artifact_sha = hashlib.sha256()
        while chunk := archive_file.read(1024 * 1024):
            artifact_sha.update(chunk)
        archive_file.seek(0)
        bundle_bytes = archive_file.seek(0, os.SEEK_END)
        archive_file.seek(0)
        if bundle_bytes > MAX_BUNDLE_BYTES:
            raise DependencyBlockedError("npm bundle exceeds its transfer limit")
        manifest = {
            "schema": 1,
            "ecosystem": "npm",
            "lock_sha256": inputs.lock_sha256,
            "bundle_sha256": artifact_sha.hexdigest(),
            "package_count": len(inputs.packages),
            "bundle_bytes": bundle_bytes,
        }
        return cast(BinaryIO, archive_file), manifest, temporary
    except Exception:
        temporary.cleanup()
        raise


def extract_npm_bundle(source: BinaryIO, destination: Path, expected_lock_sha256: str, package_count: int) -> DependencyEvidence:
    """Extract only regular files from a bounded generated node_modules archive."""
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    files = 0
    seen: set[str] = set()
    artifact_hash = hashlib.sha256()
    artifact_bytes = 0
    try:
        while chunk := source.read(1024 * 1024):
            artifact_hash.update(chunk)
            artifact_bytes += len(chunk)
            if artifact_bytes > MAX_BUNDLE_BYTES:
                raise DependencyBlockedError("npm archive exceeds its transfer limit")
    finally:
        source.seek(0)
    try:
        with tarfile.open(fileobj=source, mode="r|") as archive:
            for member in archive:
                name = member.name
                parts = name.split("/")
                if (not name.startswith("node_modules/") or any(part in {"", ".", ".."} for part in parts)
                        or "\\" in name or name.startswith("/") or name in seen):
                    raise DependencyBlockedError("npm bundle contains an unsafe or duplicate path")
                if not member.isfile() or member.size < 0:
                    raise DependencyBlockedError("npm bundle contains a link or special filesystem entry")
                seen.add(name)
                files += 1
                total += member.size
                if files > MAX_ARCHIVE_FILES or total > MAX_UNPACKED_BYTES or total > MAX_BUNDLE_BYTES:
                    raise DependencyBlockedError("npm bundle exceeds its file or byte limit")
                target = destination.joinpath(*parts[1:])
                if target.exists() or target.is_symlink():
                    raise DependencyBlockedError("npm bundle contains colliding paths")
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                if stream is None:
                    raise DependencyBlockedError("npm bundle is truncated")
                with stream, target.open("xb") as output:
                    remaining = member.size
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise DependencyBlockedError("npm bundle is truncated")
                        output.write(chunk)
                        remaining -= len(chunk)
                target.chmod(0o555 if "/.bin/" in name else 0o444)
    except (OSError, tarfile.TarError) as exc:
        raise DependencyBlockedError("npm bundle is invalid") from exc
    return DependencyEvidence(
        expected_lock_sha256,
        artifact_hash.hexdigest(),
        package_count,
        artifact_bytes,
        ecosystem="npm",
        uv_version=None,
        pip_version=None,
    )
