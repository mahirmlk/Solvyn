from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from solvyn.sandbox.dependencies import DependencyBlockedError
from solvyn.sandbox.npm_dependencies import (
    _safe_package_tarball,
    discover_npm_inputs,
    extract_npm_bundle,
    fetch_npm_bundle,
    validate_npm_pair,
)
from solvyn.sandbox.profiles import command_for_profile


def _pair(
    *, version: int = 3, url: str = "https://registry.npmjs.org/demo/-/demo-1.0.0.tgz",
    name: str = "demo", integrity_for: bytes = b"fixture",
) -> tuple[bytes, bytes]:
    digest = base64.b64encode(hashlib.sha512(integrity_for).digest()).decode()
    manifest = {"name": "fixture", "version": "1.0.0", "dependencies": {name: "^1.0.0"}}
    package = {"name": name, "version": "1.0.0", "resolved": url, "integrity": f"sha512-{digest}"}
    lock: dict[str, object] = {
        "name": "fixture", "version": "1.0.0", "lockfileVersion": version,
        "packages": {"": manifest, f"node_modules/{name}": package},
    }
    if version == 2:
        lock["dependencies"] = {name: {"version": "1.0.0", "resolved": url, "integrity": f"sha512-{digest}"}}
    return json.dumps(manifest).encode(), json.dumps(lock).encode()


def test_root_npm_lock_v3_pair_is_accepted() -> None:
    manifest, lock = _pair()
    result = validate_npm_pair(manifest, lock)
    assert len(result.packages) == 1
    assert result.packages[0].name == "demo"


def test_npm_profile_uses_declared_fixed_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, lock = _pair()
    project = json.loads(manifest)
    project["scripts"] = {"test": "node test.js", "build": "node build.js"}
    (tmp_path / "package.json").write_text(json.dumps(project), encoding="utf-8")
    (tmp_path / "package-lock.json").write_bytes(lock)
    monkeypatch.setattr("solvyn.sandbox.npm_dependencies._read_regular_file", lambda path, _maximum: path.read_bytes())
    monkeypatch.setattr("solvyn.sandbox.profiles._read_regular_file", lambda path, _maximum: path.read_bytes())
    assert command_for_profile(tmp_path, "test") == ["npm", "run", "test"]
    assert command_for_profile(tmp_path, "build") == ["npm", "run", "build"]
    with pytest.raises(DependencyBlockedError, match="must define a lint script"):
        command_for_profile(tmp_path, "lint")


def test_root_npm_lock_v2_requires_matching_legacy_tree() -> None:
    manifest, lock = _pair(version=2)
    assert len(validate_npm_pair(manifest, lock).packages) == 1
    value = json.loads(lock)
    value["dependencies"]["demo"]["version"] = "2.0.0"
    with pytest.raises(DependencyBlockedError, match="legacy dependency map disagrees"):
        validate_npm_pair(manifest, json.dumps(value).encode())


@pytest.mark.parametrize(
    ("manifest", "lock", "message"),
    [
        (b'{"name":"x","name":"y"}', b"{}", "duplicate JSON keys"),
        (_pair()[0], _pair(url="https://registry.npmjs.org:443/demo.tgz")[1], "direct HTTPS"),
        (_pair()[0], _pair(url="https://user@registry.npmjs.org/demo.tgz")[1], "direct HTTPS"),
        (_pair()[0], _pair(url="https://registry.npmjs.org/../evil.tgz")[1], "direct HTTPS"),
        (_pair()[0], _pair(url="https://registry.npmjs.org/%2e%2e/evil.tgz")[1], "direct HTTPS"),
        (_pair()[0], _pair(url="https://127.0.0.1/demo.tgz")[1], "public npm registry"),
        (_pair()[0], _pair(url="file:../demo.tgz")[1], "direct HTTPS"),
        (_pair()[0], _pair()[1].replace(b"sha512-", b"sha1-"), "SHA-512"),
        (json.dumps({"workspaces": ["packages/*"]}).encode(), _pair()[1], "workspaces"),
        (_pair()[0], _pair(name="other")[1], "disagree on dependencies"),
    ],
)
def test_npm_pair_rejects_unsafe_or_inconsistent_inputs(manifest: bytes, lock: bytes, message: str) -> None:
    with pytest.raises(DependencyBlockedError, match=message):
        validate_npm_pair(manifest, lock)


def test_npm_package_name_must_match_lock_path() -> None:
    manifest, lock = _pair()
    value = json.loads(lock)
    value["packages"]["node_modules/demo"]["name"] = "other"
    with pytest.raises(DependencyBlockedError, match="does not match its install path"):
        validate_npm_pair(manifest, json.dumps(value).encode())


def test_npm_lock_must_cover_direct_and_transitive_dependencies() -> None:
    manifest, lock = _pair()
    value = json.loads(lock)
    del value["packages"]["node_modules/demo"]
    with pytest.raises(DependencyBlockedError, match="omits a declared root dependency"):
        validate_npm_pair(manifest, json.dumps(value).encode())

    manifest, lock = _pair()
    value = json.loads(lock)
    value["packages"]["node_modules/demo"]["dependencies"] = {"missing": "^1.0.0"}
    with pytest.raises(DependencyBlockedError, match="omits a transitive dependency"):
        validate_npm_pair(manifest, json.dumps(value).encode())


def test_npm_lifecycle_packages_and_malformed_metadata_are_rejected() -> None:
    manifest, lock = _pair()
    value = json.loads(lock)
    value["packages"]["node_modules/demo"]["hasInstallScript"] = True
    with pytest.raises(DependencyBlockedError, match="lifecycle scripts"):
        validate_npm_pair(manifest, json.dumps(value).encode())
    value["packages"]["node_modules/demo"].pop("hasInstallScript")
    value["packages"]["node_modules/demo"]["dev"] = "false"
    with pytest.raises(DependencyBlockedError, match="install metadata is malformed"):
        validate_npm_pair(manifest, json.dumps(value).encode())


def test_discovery_requires_root_pair_and_rejects_nested_lock(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_bytes(_pair()[0])
    with pytest.raises(DependencyBlockedError, match="required as a pair"):
        discover_npm_inputs(tmp_path)
    (tmp_path / "package-lock.json").write_bytes(_pair()[1])
    nested = tmp_path / "packages" / "child"
    nested.mkdir(parents=True)
    (nested / "package.json").write_text("{}", encoding="utf-8")
    with pytest.raises(DependencyBlockedError, match="Nested projects"):
        discover_npm_inputs(tmp_path)


def test_package_tarball_rejects_traversal(tmp_path: Path) -> None:
    archive_path = tmp_path / "evil.tgz"
    with tarfile.open(archive_path, "w:gz") as archive:
        info = tarfile.TarInfo("package/../../escape")
        info.size = 1
        import io
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(DependencyBlockedError, match="unsafe path"):
        _safe_package_tarball(archive_path.read_bytes(), tmp_path / "package", [0, 0])


def test_package_tarball_rejects_links(tmp_path: Path) -> None:
    archive_path = tmp_path / "link.tgz"
    with tarfile.open(archive_path, "w:gz") as archive:
        info = tarfile.TarInfo("package/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../escape"
        archive.addfile(info)
    with pytest.raises(DependencyBlockedError, match="link or special"):
        _safe_package_tarball(archive_path.read_bytes(), tmp_path / "package", [0, 0])


def test_fetcher_uses_only_locked_url_verifies_sri_and_emits_safe_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_tar = io.BytesIO()
    metadata = json.dumps({"name": "demo", "version": "1.0.0"}).encode()
    with tarfile.open(fileobj=package_tar, mode="w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))
    tar_bytes = package_tar.getvalue()
    manifest, lock = _pair(integrity_for=tar_bytes)
    inputs = validate_npm_pair(manifest, lock)
    expected_url = inputs.packages[0].resolved

    class Response:
        status = 200
        headers = {"Content-Length": str(len(tar_bytes))}

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def geturl(self) -> str:
            return expected_url

        def read(self, size: int = -1) -> bytes:
            return package_tar_stream.read(size)

    package_tar_stream = io.BytesIO(tar_bytes)

    class Opener:
        def open(self, request: object, *, timeout: float):
            assert getattr(request, "full_url") == expected_url
            assert timeout <= 15
            return Response()

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: Opener())
    bundle, manifest_data, temporary = fetch_npm_bundle(inputs, proxy="http://proxy.invalid:3128")
    try:
        assert manifest_data["ecosystem"] == "npm"
        node_modules = tmp_path / "extracted-node-modules"
        evidence = extract_npm_bundle(bundle, node_modules, inputs.lock_sha256, 1)
        assert (node_modules / "demo" / "package.json").read_bytes() == metadata
        assert evidence.wheelhouse_sha256 == manifest_data["bundle_sha256"]
        assert evidence.wheelhouse_bytes == manifest_data["bundle_bytes"]
    finally:
        bundle.close()
        temporary.cleanup()


def test_fetcher_rejects_redirect_even_with_valid_lock_and_sri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, lock = _pair()
    inputs = validate_npm_pair(manifest, lock)

    class Response:
        status = 200
        headers = {"Content-Length": "7"}

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def geturl(self) -> str:
            return "https://attacker.example/redirected.tgz"

        def read(self, _size: int = -1) -> bytes:
            return b"fixture"

    class Opener:
        def open(self, *_args: object, **_kwargs: object):
            return Response()

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: Opener())
    with pytest.raises(DependencyBlockedError, match="redirected"):
        fetch_npm_bundle(inputs, proxy="http://proxy.invalid:3128")
