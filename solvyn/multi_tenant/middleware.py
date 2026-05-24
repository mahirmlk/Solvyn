import contextvars
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from sqlalchemy import select

from solvyn.db.models import Tenant
from solvyn.db.session import async_session_factory


class TenantContext:
    def __init__(self) -> None:
        self._tenant_id = contextvars.ContextVar[uuid.UUID | None]("tenant_id", default=None)
        self._tenant_slug = contextvars.ContextVar[str | None]("tenant_slug", default=None)

    @property
    def tenant_id(self) -> uuid.UUID | None:
        return self._tenant_id.get()

    @property
    def tenant_slug(self) -> str | None:
        return self._tenant_slug.get()

    def set(self, tenant_id: uuid.UUID, slug: str) -> None:
        self._tenant_id.set(tenant_id)
        self._tenant_slug.set(slug)

    def clear(self) -> None:
        self._tenant_id.set(None)
        self._tenant_slug.set(None)


tenant_context = TenantContext()


async def resolve_tenant(request: Request) -> Tenant | None:
    tenant_slug = request.headers.get("X-Tenant-Slug")

    if not tenant_slug:
        return None

    async with async_session_factory() as session:
        result = await session.execute(select(Tenant).where(Tenant.slug == tenant_slug))
        tenant = result.scalar_one_or_none()
        if tenant and tenant.is_active:
            tenant_context.set(tenant.id, tenant.slug)
            return tenant
    return None


async def tenant_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    tenant_context.clear()
    await resolve_tenant(request)
    response = await call_next(request)
    return response
