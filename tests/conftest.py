"""Test fixtures — isolate each test against a fresh temporary SQLite DB."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as db
from app.models import Base


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point app.db at a throwaway SQLite file and create the schema."""
    url = f"sqlite:///{tmp_path}/test.db"
    engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", SessionLocal)

    Base.metadata.create_all(engine)
    db.init_db()
    yield engine
    engine.dispose()
