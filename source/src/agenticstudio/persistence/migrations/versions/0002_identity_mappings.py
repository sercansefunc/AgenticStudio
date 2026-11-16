"""Persist versioned storage keys independently from opaque Opportunity IDs."""

import sqlalchemy as sa
from alembic import op

revision = "0002_identity_mappings"
down_revision = "0001_opportunities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "identity_mappings",
        sa.Column("storage_key", sa.Text(), primary_key=True),
        sa.Column("opportunity_id", sa.String(36), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("identity_mappings")
