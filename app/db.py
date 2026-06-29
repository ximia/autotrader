"""Database engine, session factory, state helpers, and lightweight migrations."""
from __future__ import annotations

import datetime as dt
import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base, BotState, FollowedTrader

log = logging.getLogger(__name__)

_settings = get_settings()

_connect_args = (
    {"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {}
)
engine = create_engine(_settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


# ─────────────────────────────────────────────────────────────────────────────
# INIT + MIGRATION
# ─────────────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create tables, run lightweight migrations, and ensure the singleton BotState row."""
    Base.metadata.create_all(engine)
    _migrate()
    with session_scope() as s:
        if s.get(BotState, 1) is None:
            s.add(BotState(id=1))


def _migrate() -> None:
    """Add new columns to existing tables without losing data.

    SQLite's ALTER TABLE only supports ADD COLUMN, which is all we need here.
    Safe to run multiple times (idempotent via column existence check).
    """
    insp = inspect(engine)

    # Map: table_name -> list of (column_definition_for_ALTER_TABLE)
    # Format: "col_name TYPE [DEFAULT value] [NOT NULL]"
    column_migrations: dict[str, list[str]] = {
        "copy_trades": [
            "confidence_score REAL",
            "slippage_pct REAL",
            "execution_latency_ms REAL",
            "signal_reasons TEXT",
            "consensus_count INTEGER",
        ],
        "traders": [
            "composite_score REAL DEFAULT 0.0",
            "roi_estimate REAL DEFAULT 0.0",
            "win_rate_proxy REAL DEFAULT 0.0",
            "sharpe_proxy REAL DEFAULT 0.0",
            "trade_count INTEGER DEFAULT 0",
            "last_scored_at DATETIME",
        ],
        "positions": [
            "peak_price REAL DEFAULT 0.0",
        ],
        "bot_state": [
            "daily_start_equity REAL DEFAULT 0.0",
            "weekly_start_equity REAL DEFAULT 0.0",
            "daily_start_day TEXT",
            "weekly_start_week TEXT",
            "consecutive_losses INTEGER DEFAULT 0",
            "last_loss_at DATETIME",
            "circuit_breaker_active BOOLEAN DEFAULT 0",
            "follow_list_refreshed_at DATETIME",
        ],
    }

    with engine.begin() as conn:
        for table, col_defs in column_migrations.items():
            if not insp.has_table(table):
                continue
            existing = {col["name"] for col in insp.get_columns(table)}
            for col_def in col_defs:
                col_name = col_def.split()[0]
                if col_name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_def}"))
                    log.info("migration: added %s.%s", table, col_name)


# ─────────────────────────────────────────────────────────────────────────────
# SESSION
# ─────────────────────────────────────────────────────────────────────────────

@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context manager."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ─────────────────────────────────────────────────────────────────────────────
# STATE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_state(session: Session) -> BotState:
    state = session.get(BotState, 1)
    if state is None:
        state = BotState(id=1)
        session.add(state)
        session.flush()
    return state


def reset_daily_spend_if_needed(state: BotState) -> None:
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    if state.spend_day != today:
        state.spend_day = today
        state.spent_today_usd = 0.0
