"""
Superseded shim: this migration used to create api_keys with a foreign
key to tenants(id) before tenants was created, so `alembic upgrade head`
failed on an empty database. All schema — including api_keys — now
lives in 0002_phase1_product_foundation.py.

For legacy deployments already at 0001 (the tables exist, thanks to
`create_all` or this revision having run against a manually created
tenants table), run:

    alembic stamp 0002_phase1_product_foundation

and verify with `alembic check` before considering any other path.

Revision ID: 0001_api_keys
Revises:
Create Date: 2026-06-14 00:00:00.000000
"""
from collections.abc import Sequence

revision: str = "0001_api_keys"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The original implementation referenced tenants before that table was
    # created, making a clean `alembic upgrade head` fail. The complete schema
    # is now created by 0002 in dependency order.
    pass


def downgrade() -> None:
    # This shim owns no schema; 0002 owns and removes api_keys.
    pass
