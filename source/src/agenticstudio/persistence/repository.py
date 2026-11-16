"""Storage-independent repository and transaction contracts."""

from contextlib import AbstractContextManager
from typing import Protocol
from uuid import UUID

from agenticstudio.domain import Opportunity


class IdentityConflictError(ValueError):
    """A storage key is already bound to a different Opportunity ID."""


class OpportunityRepository(Protocol):
    def resolve_identity(self, storage_key: str) -> UUID | None:
        """Resolve a fully versioned storage key within the current transaction."""
        ...

    def bind_identity(self, storage_key: str, opportunity_id: UUID) -> None:
        """Bind an opaque ID within the current transaction.

        Repeating a binding is idempotent; another ID raises IdentityConflictError.
        Multiple keys may reference one ID.
        """
        ...

    def get(self, identifier: UUID) -> Opportunity | None:
        """Return the stored snapshot, or None when the ID is absent."""
        ...

    def list_all(self) -> tuple[Opportunity, ...]:
        """Return every stored snapshot ordered by ID; read-only."""
        ...

    def save(self, opportunity: Opportunity) -> None:
        """Replace the snapshot by ID; repeating the same write is idempotent.

        Writes participate in the enclosing transaction. On conflicting writes,
        the last successfully committed snapshot wins; no dedupe is performed.
        """
        ...

    def delete(self, identifier: UUID) -> None:
        """Remove the snapshot and every identity binding to it; absent IDs are a no-op."""
        ...


class OpportunityStore(Protocol):
    def transaction(self) -> AbstractContextManager[OpportunityRepository]:
        """Commit on normal exit, roll back on exception; repositories cannot escape."""
        ...
