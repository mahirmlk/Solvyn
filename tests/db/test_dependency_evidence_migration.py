from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_dependency_evidence_database_constraint_rejects_null_counts() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url or not database_url.startswith("postgresql+asyncpg://"):
        pytest.skip("PostgreSQL DATABASE_URL is required for the migration constraint test")
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            definition = (
                await connection.execute(
                    text(
                        "SELECT pg_get_constraintdef(oid) "
                        "FROM pg_constraint "
                        "WHERE conrelid = 'verification_runs'::regclass "
                        "AND conname = 'ck_verification_runs_dependency_evidence_pair'"
                    )
                )
            ).scalar_one()
            assert "dependency_package_count IS NOT NULL" in definition
            assert "dependency_wheelhouse_bytes IS NOT NULL" in definition
            await connection.exec_driver_sql(
                "CREATE TEMP TABLE dependency_evidence_constraint_probe ("
                "dependency_lock_sha256 text, "
                "dependency_wheelhouse_sha256 text, "
                "dependency_runtime_fingerprint text, "
                "dependency_uv_version text, "
                "dependency_pip_version text, "
                "dependency_package_count integer, "
                "dependency_wheelhouse_bytes integer, "
                f"{definition}"
                ") ON COMMIT PRESERVE ROWS"
            )
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            "INSERT INTO dependency_evidence_constraint_probe VALUES "
                            "(:lock, :wheelhouse, :runtime, :uv, :pip, NULL, NULL)"
                        ),
                        {
                            "lock": "a" * 64,
                            "wheelhouse": "b" * 64,
                            "runtime": "c" * 64,
                            "uv": "0.13.7",
                            "pip": "26.3",
                        },
                    )
    finally:
        await engine.dispose()
