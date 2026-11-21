from __future__ import annotations

import hashlib
import io
import os
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from solvyn.sandbox.dependencies import (
    _read_regular_file,
    DependencyBlockedError,
    build_bundle_manifest,
    create_bundle_archive,
    discover_dependency_inputs,
    extract_bundle_archive,
    parse_exported_requirements,
    validate_uv_project,
)
from solvyn.sandbox.fetch_service import _render_export_pyproject, _run_tool


def _wheel(path: Path, name: str, version: str, module_text: str) -> str:
    normalized = name.replace("-", "_")
    dist_info = f"{normalized}-{version}.dist-info"
    files = {
        f"{normalized}/__init__.py": module_text.encode(),
        f"{dist_info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n".encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: solvyn-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record_rows = []
    import base64
    import csv

    for filename, payload in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
        record_rows.append((filename, f"sha256={digest}", str(len(payload))))
    record_rows.append((f"{dist_info}/RECORD", "", ""))
    output = io.StringIO()
    csv.writer(output, lineterminator="\n").writerows(record_rows)
    files[f"{dist_info}/RECORD"] = output.getvalue().encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, payload in files.items():
            archive.writestr(filename, payload)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is required for the production Linux boundary")
def test_input_reader_rejects_symlink_swap_at_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "pyproject.toml"
    target = tmp_path / "attacker.toml"
    source.write_text("trusted", encoding="utf-8")
    target.write_text("attacker", encoding="utf-8")
    real_open = os.open

    def swap_before_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if os.fspath(path) == os.fspath(source):
            source.unlink()
            source.symlink_to(target)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)
    with pytest.raises(DependencyBlockedError, match="opened safely"):
        _read_regular_file(source, 1024)


def test_requirements_accept_markers_and_multiple_hashes() -> None:
    a = "a" * 64
    b = "b" * 64
    exported = (
        f"demo-pkg==1.0 ; python_version < '3.13' --hash=sha256:{a} --hash=sha256:{b}\n"
    ).encode()
    assert len(parse_exported_requirements(exported)) == 1


def test_wheelhouse_round_trip_binds_hashes_and_allows_marker_versions(tmp_path: Path) -> None:
    wheels = tmp_path / "wheelhouse"
    wheels.mkdir()
    digest_1 = _wheel(wheels / "demo_pkg-1.0-py3-none-any.whl", "demo-pkg", "1.0", "VALUE = 1\n")
    digest_2 = _wheel(wheels / "demo_pkg-2.0-py3-none-any.whl", "demo-pkg", "2.0", "VALUE = 2\n")
    requirements = (
        f"demo-pkg==1.0 ; python_version < '3.13' --hash=sha256:{digest_1}\n"
        f"demo-pkg==2.0 ; python_version >= '3.13' --hash=sha256:{digest_2}\n"
    ).encode()
    manifest, _ = build_bundle_manifest(wheels, lock_sha256="c" * 64, requirements=requirements)
    manifest["toolchain"] = {"uv": "0.13.7", "pip": "26.3"}
    assert manifest["package_count"] == 1
    assert manifest["wheel_count"] == 2

    archive = io.BytesIO()
    create_bundle_archive(wheels, requirements, manifest, archive)
    archive.seek(0)
    extracted = tmp_path / "extracted"
    evidence = extract_bundle_archive(archive, extracted, "c" * 64)
    assert evidence.package_count == 1
    assert len(list(extracted.glob("*.whl"))) == 2


def test_wheelhouse_rejects_hash_not_allowed_by_locked_requirement(tmp_path: Path) -> None:
    wheels = tmp_path / "wheelhouse"
    wheels.mkdir()
    _wheel(wheels / "demo_pkg-1.0-py3-none-any.whl", "demo-pkg", "1.0", "VALUE = 1\n")
    requirements = (f"demo-pkg==1.0 --hash=sha256:{'a' * 64}\n").encode()
    with pytest.raises(DependencyBlockedError, match="not pinned"):
        build_bundle_manifest(wheels, lock_sha256="d" * 64, requirements=requirements)


@pytest.mark.parametrize(
    "line",
    [
        "demo-pkg @ https://example.invalid/demo.whl --hash=sha256:" + "a" * 64,
        "demo-pkg==1.0",
        "demo-pkg==1.0 --hash=md5:" + "a" * 32,
        "--index-url https://example.invalid/simple",
    ],
)
def test_requirements_reject_unsafe_forms(line: str) -> None:
    with pytest.raises(DependencyBlockedError):
        parse_exported_requirements((line + "\n").encode())


def test_fetch_export_drops_repository_build_backend(tmp_path: Path) -> None:
    marker = tmp_path / "backend-ran"
    pyproject = {
        "project": {
            "name": "demo",
            "version": "1.0",
            "requires-python": ">=3.12",
            "dependencies": ["demo-dep>=1"],
            "optional-dependencies": {"docs": ["docs-dep>=1"]},
            "scripts": {"malicious": "backend:run"},
        },
        "build-system": {
            "requires": [],
            "build-backend": "backend",
            "backend-path": ["."],
        },
        "tool": {"setuptools": {"dynamic": {"version": {"attr": "backend:run"}}}},
        "dependency-groups": {"dev": ["pytest>=8"]},
    }
    (tmp_path / "backend.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )

    sanitized = _render_export_pyproject(pyproject).decode("utf-8")
    parsed = tomllib.loads(sanitized)
    assert parsed["project"]["dependencies"] == ["demo-dep>=1"]
    assert parsed["project"]["optional-dependencies"]["docs"] == ["docs-dep>=1"]
    assert parsed["dependency-groups"]["dev"] == ["pytest>=8"]
    assert "build-system" not in parsed
    assert "tool" not in parsed
    assert "scripts" not in parsed["project"]
    assert "backend" not in sanitized
    assert not marker.exists()


def test_dynamic_project_metadata_is_rejected_before_export() -> None:
    project = {
        "project": {
            "name": "demo",
            "version": "1.0",
            "requires-python": ">=3.12",
            "dynamic": ["version"],
        }
    }
    with pytest.raises(DependencyBlockedError, match="Dynamic project metadata"):
        validate_uv_project(project, {"version": 1, "package": []})


@pytest.mark.parametrize(
    "pyproject",
    [
        "[tool.poetry]\nname='demo'\nversion='1.0'\n[tool.poetry.dev-dependencies]\npytest='^8'\n",
        "[tool.poetry]\nname='demo'\nversion='1.0'\n[tool.poetry.group.test.dependencies]\npytest='^8'\n",
        "[tool.pdm.dev-dependencies]\ntest=['pytest>=8']\n",
        "[tool.uv]\ndev-dependencies=['pytest>=8']\n",
    ],
)
def test_legacy_pyproject_dependencies_require_uv_lock(tmp_path: Path, pyproject: str) -> None:
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    with pytest.raises(DependencyBlockedError, match="supported root uv.lock"):
        discover_dependency_inputs(tmp_path, required=False)


@pytest.mark.parametrize("filename", ["setup.cfg", "setup.py"])
def test_legacy_setup_manifests_require_uv_lock(tmp_path: Path, filename: str) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='demo'\nversion='1.0'\nrequires-python='>=3.12'\n",
        encoding="utf-8",
    )
    (tmp_path / filename).write_text("# package metadata may declare dependencies\n", encoding="utf-8")
    with pytest.raises(DependencyBlockedError, match="Python project and dependency manifests require a supported root uv.lock"):
        discover_dependency_inputs(tmp_path, required=False)


def test_build_system_requirements_need_uv_lock(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='demo'\nversion='1.0'\nrequires-python='>=3.12'\n"
        "[build-system]\nrequires=['setuptools>=70']\nbuild-backend='setuptools.build_meta'\n",
        encoding="utf-8",
    )
    with pytest.raises(DependencyBlockedError, match="Python project and dependency manifests require a supported root uv.lock"):
        discover_dependency_inputs(tmp_path, required=False)


@pytest.mark.parametrize(
    "relative_path",
    [
        "requirements-prod.txt",
        "requirements-ci.in",
        "requirements-prod/ci.in",
        "ci/requirements.txt",
        "tools/build/constraints-ci.txt",
        "requirements/ci.txt",
        "constraints-prod.txt",
        "constraints/test.in",
    ],
)
def test_requirements_family_requires_uv_lock(tmp_path: Path, relative_path: str) -> None:
    manifest = tmp_path / relative_path
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("demo-dep>=1\n", encoding="utf-8")
    with pytest.raises(DependencyBlockedError, match="Python project and dependency manifests require a supported root uv.lock"):
        discover_dependency_inputs(tmp_path, required=False)


def test_fetch_subprocess_can_write_wheels_larger_than_two_megabytes(tmp_path: Path) -> None:
    artifact = tmp_path / "large-wheel.bin"
    _run_tool(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('large-wheel.bin').write_bytes(b'x' * (3 * 1024 * 1024))",
        ],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_stdout=False,
    )
    assert artifact.stat().st_size == 3 * 1024 * 1024
