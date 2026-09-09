from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from recantor.settings import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(settings.database_url, pool_pre_ping=True)


async def check_database() -> None:
    async with get_engine().connect() as connection:
        await connection.execute(text("SELECT 1"))
