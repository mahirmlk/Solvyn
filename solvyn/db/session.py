import uuid
from collections.abc import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.orm import SessionTransaction

from solvyn.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
    pool_recycle=settings.db_pool_recycle_seconds,
)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(SyncSession, "after_begin")
def _set_postgres_tenant_context(
    session: SyncSession, transaction: SessionTransaction, connection: Connection
) -> None:
    """Bind authenticated request context to each PostgreSQL transaction.

    PostgreSQL RLS policies read this transaction-local value. Setting it on
    transaction start keeps pooled connections from retaining another
    request's tenant and also covers sessions opened outside ``get_session``.
    Unscoped maintenance/authentication work deliberately receives no value;
    policies then fail closed for protected tables.
    """
    del session, transaction
    if connection.dialect.name != "postgresql":
        return
    # Import lazily: tenant middleware uses this module's session factory.
    from solvyn.multi_tenant.middleware import tenant_context

    tenant_id = tenant_context.tenant_id
    if tenant_id is None:
        return
    connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})


async def get_session() -> AsyncGenerator[AsyncSession]:
    async with async_session_factory() as session:
        yield session


async def bind_tenant_scope(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Set transaction-local scope after a route has authorized a tenant row.

    Some compatibility credentials authorize the configured default tenant
    without putting that tenant UUID in request context. Callers must first
    resolve and authorize the tenant from a resource they already own.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
