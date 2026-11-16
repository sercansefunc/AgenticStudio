"""Create canonical Opportunity snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0001_opportunities"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "opportunities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.CheckConstraint("payload_version = 1", name="ck_opportunities_payload_version"),
    )


def downgrade() -> None:
    op.drop_table("opportunities")
