"""Support an injected connection or an explicitly configured CLI URL."""

from alembic import context
from sqlalchemy import Connection, create_engine, pool

from agenticstudio.persistence.schema import metadata

config = context.config


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    connection = config.attributes.get("connection")
    if connection is not None:
        run_migrations(connection)
    else:
        url = config.get_main_option("sqlalchemy.url")
        if not url:
            raise ValueError("Set sqlalchemy.url explicitly before running migrations")
        engine = create_engine(url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                run_migrations(connection)
        finally:
            engine.dispose()
