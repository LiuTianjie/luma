"""Allow retained application volumes to keep their Luma binding.

Revision ID: 20260711_0012
Revises: 20260711_0011
Create Date: 2026-07-14
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260711_0012"
down_revision: str | None = "20260711_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_application_volumes_provisioning_binding"),
        "application_volumes",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_application_volumes_provisioning_binding"),
        "application_volumes",
        "((luma_volume_ref IS NULL) = (provisioned_at IS NULL)) AND "
        "(status <> 'ready' OR luma_volume_ref IS NOT NULL)",
    )


def downgrade() -> None:
    # The old invariant cannot represent a retained volume that still owns its
    # Luma volume. Restoring it would either fail or discard the only durable
    # binding needed by an operator to recover/delete retained data.
    op.execute(
        "UPDATE application_volumes SET status = 'ready' "
        "WHERE luma_volume_ref IS NOT NULL AND provisioned_at IS NOT NULL"
    )
    op.drop_constraint(
        op.f("ck_application_volumes_provisioning_binding"),
        "application_volumes",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_application_volumes_provisioning_binding"),
        "application_volumes",
        "(status = 'ready') = "
        "(luma_volume_ref IS NOT NULL AND provisioned_at IS NOT NULL)",
    )
