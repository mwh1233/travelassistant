"""PostgreSQL checkpointer lifecycle management."""

import asyncio
import sys
from contextlib import asynccontextmanager
from typing import Optional

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from app.config import settings
from app.utils.logger import app_logger


class CheckpointerManager:
    """Singleton manager for the LangGraph Postgres checkpointer."""

    _instance: Optional["CheckpointerManager"] = None
    _lock = asyncio.Lock()

    def __init__(self) -> None:
        self.pool: Optional[AsyncConnectionPool] = None
        self.checkpointer: Optional[AsyncPostgresSaver] = None

    @classmethod
    async def get_instance(cls) -> "CheckpointerManager":
        """Return the initialized singleton instance."""

        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    instance = cls()
                    await instance.initialize()
                    cls._instance = instance
        return cls._instance

    @classmethod
    async def reset_instance(cls) -> None:
        """Close and clear the singleton, mainly for tests."""

        if cls._instance is not None:
            await cls._instance.close()
        cls._instance = None

    async def initialize(self) -> None:
        """Initialize the connection pool and saver tables."""

        if self.checkpointer is not None:
            app_logger.warning("Checkpointer is already initialized; skipping")
            return

        try:
            app_logger.info("Initializing PostgreSQL checkpointer...")
            loop = asyncio.get_running_loop()
            if sys.platform == "win32" and "Proactor" in type(loop).__name__:
                raise RuntimeError(
                    "PostgreSQL async checkpointer requires SelectorEventLoop on Windows. "
                    "Use app/run.py or WindowsSelectorEventLoopPolicy."
                )
            self.pool = AsyncConnectionPool(
                conninfo=settings.database_url,
                min_size=1,
                max_size=20,
                timeout=5,
                open=False,
                kwargs={"autocommit": True, "prepare_threshold": 0},
            )
            await self.pool.open()
            self.checkpointer = AsyncPostgresSaver(self.pool)
            await self.checkpointer.setup()
            app_logger.info("PostgreSQL checkpointer initialized")
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        """Close the connection pool."""

        if self.pool is not None:
            await self.pool.close()
            self.pool = None
        self.checkpointer = None

    def get_checkpointer(self) -> AsyncPostgresSaver:
        """Return the initialized checkpointer."""

        if self.checkpointer is None:
            raise RuntimeError("Checkpointer is not initialized")
        return self.checkpointer


async def get_checkpointer() -> AsyncPostgresSaver:
    """Return the global checkpointer instance."""

    manager = await CheckpointerManager.get_instance()
    return manager.get_checkpointer()


@asynccontextmanager
async def checkpointer_lifespan():
    """FastAPI lifespan helper for the checkpointer."""

    manager = await CheckpointerManager.get_instance()
    try:
        yield manager.get_checkpointer()
    finally:
        await manager.close()
