"""Safety checks for the host-side PostgreSQL backup wrapper."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="backup wrapper behavior is exercised in Linux CI",
)
@pytest.mark.parametrize("database_url", [None, ""])
def test_backup_requires_explicit_database_url_before_side_effects(
    tmp_path: Path,
    database_url: str | None,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    backup_dir = tmp_path / "backups"
    environment = os.environ.copy()
    if database_url is None:
        environment.pop("DATABASE_URL", None)
    else:
        environment["DATABASE_URL"] = database_url

    result = subprocess.run(
        ["bash", "scripts/backup-db.sh", str(backup_dir)],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert "Set DATABASE_URL explicitly" in result.stderr
    assert not backup_dir.exists()
