from logging.config import fileConfig

from alembic import context

from app.db import Base, engine
import app.models  # noqa: F401  (registers tables)

if context.config.config_file_name:
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
