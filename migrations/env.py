"""Alembic environment.

The consolidation tool always runs migrations *online* with a connection injected by
:mod:`consolidation.pipeline` via ``config.attributes["connection"]`` — so the refactor
executes inside the application's setup transaction. Offline (``--sql``) mode
and autogenerate are not used.
"""

from __future__ import annotations

from alembic import context

from consolidation import db_upgrade

config = context.config


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is None:
        raise RuntimeError(
            "consolidation runs Alembic with an injected connection "
            "(config.attributes['connection']); the bare CLI is unsupported"
        )

    context.configure(
        connection=connection,
        target_metadata=db_upgrade.metadata,
        transaction_per_migration=False,
        transactional_ddl=False,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported")

run_migrations_online()
