"""Small persistence adapter for local SQLite and Cloudflare D1."""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx


SCHEMA = """
CREATE TABLE IF NOT EXISTS clinic_sessions (
    session_id TEXT PRIMARY KEY,
    clinic_id TEXT NOT NULL,
    language_code TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    last_transcript TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS transcript_events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    clinic_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    transcript TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES clinic_sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_transcript_events_session
    ON transcript_events(session_id, created_at);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EdgePersistence:
    """Persist clinical stream metadata without storing raw audio."""

    def __init__(self, sqlite_path: str | None = None) -> None:
        self.sqlite_path = sqlite_path or os.getenv("EDGE_SQLITE_PATH", "edge_sync.sqlite3")
        self.d1_url = os.getenv("CLOUDFLARE_D1_API_URL", "")
        self.d1_token = os.getenv("CLOUDFLARE_D1_API_TOKEN", "")

    @property
    def backend(self) -> str:
        return "d1" if self.d1_url and self.d1_token else "sqlite"

    def _sqlite(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.sqlite_path)
        connection.executescript(SCHEMA)
        return connection

    async def _d1_execute(self, sql: str, params: list[Any]) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                self.d1_url,
                headers={"Authorization": f"Bearer {self.d1_token}"},
                json={"sql": sql, "params": params},
            )
            response.raise_for_status()

    async def initialize(self) -> None:
        if self.backend == "d1":
            for statement in filter(None, (part.strip() for part in SCHEMA.split(";"))):
                await self._d1_execute(statement, [])
            return
        connection = self._sqlite()
        connection.commit()
        connection.close()

    async def start_session(self, *, clinic_id: str, language_code: str) -> str:
        session_id = str(uuid.uuid4())
        started_at = utc_now()
        sql = (
            "INSERT INTO clinic_sessions "
            "(session_id, clinic_id, language_code, started_at) VALUES (?, ?, ?, ?)"
        )
        params = [session_id, clinic_id, language_code, started_at]
        if self.backend == "d1":
            await self._d1_execute(sql, params)
        else:
            connection = self._sqlite()
            connection.execute(sql, params)
            connection.commit()
            connection.close()
        return session_id

    async def record_transcript(
        self,
        *,
        session_id: str,
        clinic_id: str,
        transcript: str,
        event_type: str,
    ) -> None:
        created_at = utc_now()
        event_sql = (
            "INSERT INTO transcript_events "
            "(event_id, session_id, clinic_id, event_type, transcript, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)"
        )
        event_params = [str(uuid.uuid4()), session_id, clinic_id, event_type, transcript, created_at]
        update_sql = (
            "UPDATE clinic_sessions SET last_transcript = ?, ended_at = "
            "CASE WHEN ? = 'final' THEN ? ELSE ended_at END WHERE session_id = ?"
        )
        update_params = [transcript, event_type, created_at, session_id]
        if self.backend == "d1":
            await self._d1_execute(event_sql, event_params)
            await self._d1_execute(update_sql, update_params)
            return
        connection = self._sqlite()
        connection.execute(event_sql, event_params)
        connection.execute(update_sql, update_params)
        connection.commit()
        connection.close()


persistence = EdgePersistence()
