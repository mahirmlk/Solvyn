"""Create the single-tenant fixture required by CI workflow integration tests."""

import asyncio

from sqlalchemy import select

from solvyn.db.models import Tenant
from solvyn.db.session import async_session_factory, engine


async def main() -> None:
    async with async_session_factory() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == "test-tenant"))).scalar_one_or_none()
        if tenant is None:
            db.add(Tenant(name="CI Test Tenant", slug="test-tenant", api_key_hash="ci-test-key", role="user"))
            await db.commit()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
