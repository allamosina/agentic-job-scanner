from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from .config import Settings


def database(settings: Settings):
    settings.require_database()
    url = settings.database_url.replace("postgres://", "postgresql+psycopg://", 1)
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url, pool_pre_ping=True, pool_size=3, max_overflow=1, pool_recycle=300)


def sessions(engine):
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def transaction_lock(factory, name: str):
    # Transaction-scoped locks also work with a transaction-mode PostgreSQL pooler.
    with factory.begin() as session:
        acquired = session.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"), {"key": name})
        yield session if acquired else None
