"""Storage schema; canonical values use Pydantic's lossless JSON representation."""

from sqlalchemy import CheckConstraint, Column, Integer, MetaData, String, Table, Text

metadata = MetaData()
opportunities = Table(
    "opportunities",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("payload_version", Integer, nullable=False),
    Column("payload", Text, nullable=False),
    CheckConstraint("payload_version = 1", name="ck_opportunities_payload_version"),
)

identity_mappings = Table(
    "identity_mappings",
    metadata,
    Column("storage_key", Text, primary_key=True),
    Column("opportunity_id", String(36), nullable=False),
)
