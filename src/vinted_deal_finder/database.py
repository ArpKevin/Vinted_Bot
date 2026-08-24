from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from .models import Listing
from .scoring import DealEvaluation


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


@dataclass(frozen=True, slots=True)
class PendingAlert:
    id: int
    provider: str
    listing_id: str
    watch_id: str
    payload: dict[str, Any]
    attempt_count: int


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self._connection is not None

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("database is not initialized")
        return self._connection

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        self._connection = connection
        async with self._lock:
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.execute("PRAGMA foreign_keys=ON")
            await connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS seen_listings (
                    provider TEXT NOT NULL,
                    listing_id TEXT NOT NULL,
                    watch_id TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_score REAL,
                    listing_json TEXT NOT NULL,
                    alerted_at TEXT,
                    delivery_status TEXT NOT NULL,
                    last_error TEXT,
                    PRIMARY KEY (provider, listing_id)
                );

                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    listing_id TEXT NOT NULL,
                    watch_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    discord_message_id TEXT,
                    payload_json TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (provider, listing_id)
                );

                CREATE TABLE IF NOT EXISTS provider_cursors (
                    watch_id TEXT PRIMARY KEY,
                    cursor TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS service_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);
                CREATE INDEX IF NOT EXISTS idx_seen_last_seen ON seen_listings(last_seen_at);
                """
            )
            await connection.commit()

    async def close(self) -> None:
        if self._connection is None:
            return
        async with self._lock:
            await self._connection.close()
            self._connection = None

    async def get_cursor(self, watch_id: str) -> str | None:
        connection = self._require_connection()
        async with self._lock:
            cursor = await connection.execute(
                "SELECT cursor FROM provider_cursors WHERE watch_id = ?", (watch_id,)
            )
            row = await cursor.fetchone()
            await cursor.close()
        return None if row is None else row["cursor"]

    async def set_cursor(self, watch_id: str, cursor: str | None) -> None:
        connection = self._require_connection()
        now = iso_now()
        async with self._lock:
            await connection.execute(
                """
                INSERT INTO provider_cursors(watch_id, cursor, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(watch_id) DO UPDATE SET
                    cursor = excluded.cursor,
                    updated_at = excluded.updated_at
                """,
                (watch_id, cursor, now),
            )
            await connection.commit()

    async def record_seen(
        self,
        listing: Listing,
        watch_id: str,
        evaluation: DealEvaluation,
    ) -> None:
        connection = self._require_connection()
        now = iso_now()
        payload = listing.model_dump_json()
        initial_status = "pending" if evaluation.qualifies else "not_qualified"
        async with self._lock:
            await connection.execute(
                """
                INSERT INTO seen_listings(
                    provider, listing_id, watch_id, first_seen_at, last_seen_at,
                    last_score, listing_json, delivery_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, listing_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    last_score = excluded.last_score,
                    listing_json = excluded.listing_json,
                    watch_id = CASE
                        WHEN seen_listings.alerted_at IS NULL THEN excluded.watch_id
                        ELSE seen_listings.watch_id
                    END,
                    delivery_status = CASE
                        WHEN seen_listings.alerted_at IS NOT NULL THEN seen_listings.delivery_status
                        WHEN excluded.delivery_status = 'pending' THEN 'pending'
                        ELSE seen_listings.delivery_status
                    END
                """,
                (
                    listing.provider,
                    listing.listing_id,
                    watch_id,
                    now,
                    now,
                    evaluation.final_score,
                    payload,
                    initial_status,
                ),
            )
            await connection.commit()

    async def was_alerted(self, provider: str, listing_id: str) -> bool:
        connection = self._require_connection()
        async with self._lock:
            cursor = await connection.execute(
                """
                SELECT 1 FROM alerts
                WHERE provider = ? AND listing_id = ? AND status = 'sent'
                """,
                (provider, listing_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return row is not None

    async def queue_alert(
        self,
        provider: str,
        listing_id: str,
        watch_id: str,
        payload: dict[str, Any],
    ) -> None:
        connection = self._require_connection()
        now = iso_now()
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            await connection.execute(
                """
                INSERT INTO alerts(
                    provider, listing_id, watch_id, status, payload_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?)
                ON CONFLICT(provider, listing_id) DO UPDATE SET
                    watch_id = CASE
                        WHEN alerts.status = 'sent' THEN alerts.watch_id
                        ELSE excluded.watch_id
                    END,
                    payload_json = CASE
                        WHEN alerts.status = 'sent' THEN alerts.payload_json
                        ELSE excluded.payload_json
                    END,
                    status = CASE WHEN alerts.status = 'sent' THEN 'sent' ELSE 'pending' END,
                    updated_at = excluded.updated_at
                """,
                (provider, listing_id, watch_id, payload_json, now, now),
            )
            await connection.execute(
                """
                UPDATE seen_listings SET delivery_status = 'pending', last_error = NULL
                WHERE provider = ? AND listing_id = ? AND alerted_at IS NULL
                """,
                (provider, listing_id),
            )
            await connection.commit()

    async def pending_alerts(self, limit: int = 100) -> list[PendingAlert]:
        connection = self._require_connection()
        async with self._lock:
            cursor = await connection.execute(
                """
                SELECT id, provider, listing_id, watch_id, payload_json, attempt_count
                FROM alerts
                WHERE status IN ('pending', 'failed')
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [
            PendingAlert(
                id=row["id"],
                provider=row["provider"],
                listing_id=row["listing_id"],
                watch_id=row["watch_id"],
                payload=json.loads(row["payload_json"]),
                attempt_count=row["attempt_count"],
            )
            for row in rows
        ]

    async def mark_alert_sent(self, alert: PendingAlert, message_id: str | None) -> None:
        connection = self._require_connection()
        now = iso_now()
        async with self._lock:
            await connection.execute("BEGIN")
            try:
                await connection.execute(
                    """
                    UPDATE alerts SET status = 'sent', attempt_count = attempt_count + 1,
                        discord_message_id = ?, last_error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (message_id, now, alert.id),
                )
                await connection.execute(
                    """
                    UPDATE seen_listings
                    SET delivery_status = 'sent', alerted_at = ?, last_error = NULL
                    WHERE provider = ? AND listing_id = ?
                    """,
                    (now, alert.provider, alert.listing_id),
                )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise

    async def mark_alert_failed(self, alert: PendingAlert, error: str) -> None:
        connection = self._require_connection()
        now = iso_now()
        sanitized = error[:1000]
        async with self._lock:
            await connection.execute("BEGIN")
            try:
                await connection.execute(
                    """
                    UPDATE alerts SET status = 'failed', attempt_count = attempt_count + 1,
                        last_error = ?, updated_at = ? WHERE id = ?
                    """,
                    (sanitized, now, alert.id),
                )
                await connection.execute(
                    """
                    UPDATE seen_listings SET delivery_status = 'failed', last_error = ?
                    WHERE provider = ? AND listing_id = ?
                    """,
                    (sanitized, alert.provider, alert.listing_id),
                )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise

    async def pending_alert_count(self) -> int:
        connection = self._require_connection()
        async with self._lock:
            cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM alerts WHERE status IN ('pending', 'failed')"
            )
            row = await cursor.fetchone()
            await cursor.close()
        return int(row["count"] if row is not None else 0)

    async def set_service_state(self, key: str, value: str) -> None:
        connection = self._require_connection()
        now = iso_now()
        async with self._lock:
            await connection.execute(
                """
                INSERT INTO service_state(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value[:2000], now),
            )
            await connection.commit()

    async def prune_seen(self, retention_days: int) -> int:
        connection = self._require_connection()
        cutoff = (utc_now() - timedelta(days=retention_days)).isoformat()
        async with self._lock:
            cursor = await connection.execute(
                """
                DELETE FROM seen_listings
                WHERE last_seen_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM alerts
                      WHERE alerts.provider = seen_listings.provider
                        AND alerts.listing_id = seen_listings.listing_id
                        AND alerts.status IN ('pending', 'failed')
                  )
                """,
                (cutoff,),
            )
            removed = cursor.rowcount
            await cursor.close()
            await connection.commit()
        return max(removed, 0)
