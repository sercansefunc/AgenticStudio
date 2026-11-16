"""SQLite snapshot storage with explicit migration and transaction lifetimes."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, Connection, create_engine, delete, select
from sqlalchemy.dialects.sqlite import insert

from agenticstudio.domain import Opportunity

from .repository import IdentityConflictError, OpportunityRepository
from .schema import identity_mappings, opportunities


class _SQLiteRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._active = True

    def _require_active(self) -> None:
        if not self._active:
            raise RuntimeError("Repository transaction has ended")

    def resolve_identity(self, storage_key: str) -> UUID | None:
        self._require_active()
        identifier = self._connection.execute(
            select(identity_mappings.c.opportunity_id)
            .where(identity_mappings.c.storage_key == storage_key)
        ).scalar_one_or_none()
        return UUID(identifier) if identifier is not None else None

    def bind_identity(self, storage_key: str, opportunity_id: UUID) -> None:
        self._require_active()
        self._connection.execute(
            insert(identity_mappings).values(
                storage_key=storage_key, opportunity_id=str(opportunity_id),
            ).on_conflict_do_nothing(index_elements=[identity_mappings.c.storage_key])
        )
        if self.resolve_identity(storage_key) != opportunity_id:
            raise IdentityConflictError(f"Storage key already bound to another ID: {storage_key!r}")

    def get(self, identifier: UUID) -> Opportunity | None:
        self._require_active()
        row = self._connection.execute(
            select(opportunities.c.payload_version, opportunities.c.payload)
            .where(opportunities.c.id == str(identifier))
        ).one_or_none()
        if row is None:
            return None
        if row.payload_version != 1:
            raise ValueError(f"Unsupported Opportunity payload version: {row.payload_version}")
        opportunity = Opportunity.model_validate_json(row.payload)
        if opportunity.id != identifier:
            raise ValueError("Stored Opportunity ID does not match repository key")
        return opportunity

    def list_all(self) -> tuple[Opportunity, ...]:
        self._require_active()
        ids = self._connection.execute(
            select(opportunities.c.id).order_by(opportunities.c.id)).scalars().all()
        found = (self.get(UUID(identifier)) for identifier in ids)
        return tuple(item for item in found if item is not None)

    def save(self, opportunity: Opportunity) -> None:
        self._require_active()
        statement = insert(opportunities).values(
            id=str(opportunity.id), payload_version=1, payload=opportunity.model_dump_json(),
        )
        self._connection.execute(statement.on_conflict_do_update(
            index_elements=[opportunities.c.id],
            set_={"payload_version": statement.excluded.payload_version,
                  "payload": statement.excluded.payload},
        ))

    def delete(self, identifier: UUID) -> None:
        self._require_active()
        self._connection.execute(delete(identity_mappings)
                                 .where(identity_mappings.c.opportunity_id == str(identifier)))
        self._connection.execute(delete(opportunities).where(opportunities.c.id == str(identifier)))


class SQLiteOpportunityStore:
    def __init__(self, database: str | Path) -> None:
        """Use a caller-supplied file path, or ':memory:' for ephemeral storage.

        Construction never creates tables or runs migrations. Parent directories
        must already exist. Each transaction obtains its own connection.
        """
        self._engine = create_engine(URL.create("sqlite+pysqlite", database=str(database)))

    def migrate(self) -> None:
        """Explicitly upgrade to Alembic head; safe to repeat at startup/deployment."""
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        with self._engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    @contextmanager
    def transaction(self) -> Iterator[OpportunityRepository]:
        with self._engine.begin() as connection:
            repository = _SQLiteRepository(connection)
            try:
                yield repository
            finally:
                repository._active = False

    def close(self) -> None:
        """Release pooled connections; callers must first finish all transactions."""
        self._engine.dispose()
