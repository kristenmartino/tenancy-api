"""
Database layer: SQLAlchemy 2.0 async, defaults to SQLite locally.

The DATABASE_URL env var switches drivers — set to a postgresql+asyncpg URL
for Neon (the README's prod target). SQLite is the dev default so nothing
external needs to be running for `uvicorn app:app` to work.
"""
from __future__ import annotations

import datetime as dt
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///tenancy.db")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class LeaseRecord(Base):
    __tablename__ = "leases"

    lease_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    pdf_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=lambda: dt.datetime.now(dt.UTC)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime,
        default=lambda: dt.datetime.now(dt.UTC),
        onupdate=lambda: dt.datetime.now(dt.UTC),
    )


class ExceptionRecord(Base):
    __tablename__ = "exceptions"

    exception_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    lease_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("leases.lease_id", ondelete="CASCADE"), index=True
    )
    field_path: Mapped[str] = mapped_column(Text)
    exception_type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16), index=True)
    description: Mapped[str] = mapped_column(Text)
    suggested_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)
    correction: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=lambda: dt.datetime.now(dt.UTC)
    )


async def init_db() -> None:
    """Create tables if they don't exist. Idempotent; safe to call at every boot."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields a session, commits/rollbacks around the request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
