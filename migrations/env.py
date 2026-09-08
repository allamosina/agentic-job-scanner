from alembic import context

from job_monitor.config import Settings
from job_monitor.db import database
from job_monitor.models import Base

settings = Settings.from_env()
if context.is_offline_mode():
    context.configure(url="postgresql+psycopg://", target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = database(settings)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
