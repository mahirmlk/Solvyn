import contextvars
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from sqlalchemy import select

from solvyn.db.models import Tenant, TenantRole
from solvyn.db.session import async_session_factory


class TenantContext:
    def __init__(self) -> None:
        self._tenant_id = contextvars.ContextVar[uuid.UUID | None]("tenant_id", default=None)
        self._tenant_slug = contextvars.ContextVar[str | None]("tenant_slug", default=None)
        self._tenant_role = contextvars.ContextVar[str]("tenant_role", default=TenantRole.VIEWER)
        # Identity bound to the authenticated principal (API key owner or JWT user).
        # request_tenant_id is the tenant resolved from the X-Tenant-Slug header;
        # authenticated_tenant_id is who proved possession of the credential.
        # They must agree before any tenant-scoped data is served.
        self._authenticated_tenant_id = contextvars.ContextVar[uuid.UUID | None]("authenticated_tenant_id", default=None)
        self._user_id = contextvars.ContextVar[uuid.UUID | None]("user_id", default=None)

    @property
    def tenant_id(self) -> uuid.UUID | None:
        return self._tenant_id.get()

    @property
    def tenant_slug(self) -> str | None:
        return self._tenant_slug.get()

    @property
    def tenant_role(self) -> str:
        return self._tenant_role.get()

    @property
    def authenticated_tenant_id(self) -> uuid.UUID | None:
        return self._authenticated_tenant_id.get()

    @property
    def user_id(self) -> uuid.UUID | None:
        return self._user_id.get()

    @property
    def is_admin(self) -> bool:
        return self._tenant_role.get() == TenantRole.ADMIN

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated_tenant_id.get() is not None

    def set(
        self,
        tenant_id: uuid.UUID,
        slug: str,
        role: str = TenantRole.USER,
        *,
        authenticated_tenant_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
    ) -> None:
        self._tenant_id.set(tenant_id)
        self._tenant_slug.set(slug)
        self._tenant_role.set(role)
        self._authenticated_tenant_id.set(authenticated_tenant_id if authenticated_tenant_id is not None else tenant_id)
        if user_id is not None:
            self._user_id.set(user_id)

    def set_authenticated(self, tenant_id: uuid.UUID | None, user_id: uuid.UUID | None = None) -> None:
        if tenant_id is not None:
            self._authenticated_tenant_id.set(tenant_id)
        if user_id is not None:
            self._user_id.set(user_id)

    def clear(self) -> None:
        self._tenant_id.set(None)
        self._tenant_slug.set(None)
        self._tenant_role.set(TenantRole.VIEWER)
        self._authenticated_tenant_id.set(None)
        self._user_id.set(None)


tenant_context = TenantContext()


async def resolve_tenant(request: Request) -> Tenant | None:
    tenant_slug = request.headers.get("X-Tenant-Slug")

    if not tenant_slug:
        return None

    async with async_session_factory() as session:
        result = await session.execute(select(Tenant).where(Tenant.slug == tenant_slug))
        tenant = result.scalar_one_or_none()
        if tenant and tenant.is_active:
            tenant_context.set(tenant.id, tenant.slug, tenant.role or TenantRole.USER)
            return tenant
    return None


async def tenant_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    tenant_context.clear()
    await resolve_tenant(request)
    try:
        response = await call_next(request)
    finally:
        tenant_context.clear()
    return response
