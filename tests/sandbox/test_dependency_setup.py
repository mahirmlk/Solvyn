from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from solvyn.sandbox.dependencies import (
    DependencyBlockedError,
    build_bundle_manifest,
    create_bundle_archive,
    extract_bundle_archive,
    parse_exported_requirements,
)


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
