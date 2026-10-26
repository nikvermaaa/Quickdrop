from contextlib import asynccontextmanager
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from app.config import settings
from app.models import Base

from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode

# Support SQLite or Postgres async dialect
db_url = settings.DATABASE_URL
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
elif db_url.startswith("postgresql://") and not db_url.startswith("postgresql+asyncpg://"):
    db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

# Handle SSL and clean query params for asyncpg (e.g. Neon, Supabase)
connect_args = {}
if "asyncpg" in db_url:
    parsed = urlsplit(db_url)
    query_params = parse_qs(parsed.query)
    ssl_required = False
    if "sslmode" in query_params:
        mode = query_params.pop("sslmode")[0]
        if mode in ("require", "verify-ca", "verify-full"):
            ssl_required = True
    query_params.pop("channel_binding", None)
    if ssl_required:
        connect_args["ssl"] = True
    clean_query = urlencode(query_params, doseq=True)
    db_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, clean_query, parsed.fragment))

engine = create_async_engine(
    db_url,
    echo=False,
    future=True,
    pool_pre_ping=True,
    connect_args=connect_args,
)

async_session = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
