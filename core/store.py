"""SQLite delivery journal, isolated conversations, and durable human handoff."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
from typing import Iterator


class StateStore:
    def __init__(self, db_path: str | Path = "data/service.db"):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        self._connection.row_factory = sqlite3.Row
        with self._transaction() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS service_events (
                    event_key TEXT PRIMARY KEY, conversation_key TEXT NOT NULL,
                    platform TEXT NOT NULL, shop_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, message_id TEXT NOT NULL, item_id TEXT NOT NULL,
                    text TEXT NOT NULL, reply TEXT, action TEXT, intent TEXT,
                    status TEXT NOT NULL DEFAULT 'processing', error TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS service_conversation_time
                    ON service_events(conversation_key, created_at);
                CREATE TABLE IF NOT EXISTS service_handoffs (
                    conversation_key TEXT PRIMARY KEY, paused INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
            """)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            with self._connection:
                yield self._connection

    def claim(self, event: dict) -> bool:
        with self._transaction() as conn:
            result = conn.execute("""
                INSERT OR IGNORE INTO service_events
                (event_key,conversation_key,platform,shop_id,user_id,conversation_id,message_id,item_id,text)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, tuple(event[key] for key in ["event_key", "conversation_key", "platform", "shop_id", "user_id", "conversation_id", "message_id", "item_id", "text"]))
            return result.rowcount == 1

    def bind_product(self, event_key: str, item_id: str) -> None:
        """Attach an unambiguous name match to its knowledge record."""
        with self._transaction() as conn:
            conn.execute("UPDATE service_events SET item_id=? WHERE event_key=? AND item_id=''", (item_id, event_key))

    def complete(self, event_key: str, *, reply: str | None, action: str, intent: str) -> None:
        status = "generated" if reply else "ignored"
        with self._transaction() as conn:
            conn.execute("""UPDATE service_events SET reply=?, action=?, intent=?, status=?,
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE event_key=? AND status='processing'
                """, (reply, action, intent, status, event_key))

    def mark_sent(self, event_key: str) -> bool:
        with self._transaction() as conn:
            cursor = conn.execute("""UPDATE service_events SET status='sent',error=NULL,
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE event_key=? AND status='generated'""", (event_key,))
            return cursor.rowcount == 1

    def mark_failed(self, event_key: str, error: str = "delivery_failed") -> bool:
        # Persist small codes only: exceptions can contain credential-bearing URLs.
        # Failed delivery isn't automatically retried; it may already have arrived.
        safe_error = error if error in {"delivery_failed", "generation_failed", "delivery_uncertain", "cancelled"} else "delivery_failed"
        with self._transaction() as conn:
            cursor = conn.execute("""UPDATE service_events SET status='failed',error=?,
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE event_key=? AND status IN ('generated','processing')""", (safe_error, event_key))
            return cursor.rowcount == 1

    def get_event(self, event_key: str) -> dict | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM service_events WHERE event_key=?", (event_key,)).fetchone()
        return dict(row) if row else None

    def list_events(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            rows = self._connection.execute("SELECT * FROM service_events ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def history(self, conversation_key: str, item_id: str = "", limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._connection.execute("""SELECT text, reply, intent, status, item_id FROM service_events
                WHERE conversation_key=? AND item_id=? AND status <> 'processing'
                ORDER BY rowid DESC LIMIT ?""", (conversation_key, item_id, max(1, min(int(limit), 100)))).fetchall()
        messages = []
        for row in reversed(rows):
            messages.append({"role": "user", "content": row["text"], "intent": row["intent"]})
            # Never claim an unconfirmed generated response was delivered.
            if row["reply"] and row["status"] == "sent":
                messages.append({"role": "assistant", "content": row["reply"], "intent": row["intent"]})
        return messages

    def bargain_count(self, conversation_key: str, item_id: str) -> int:
        with self._lock:
            row = self._connection.execute("""SELECT COUNT(*) FROM service_events
                WHERE conversation_key=? AND item_id=? AND intent='bargain' AND status='sent'""", (conversation_key, item_id)).fetchone()
        return row[0]

    def set_handoff(self, conversation_key: str, paused: bool = True, reason: str = "manual") -> None:
        with self._transaction() as conn:
            conn.execute("""INSERT INTO service_handoffs(conversation_key,paused,reason) VALUES (?,?,?)
                ON CONFLICT(conversation_key) DO UPDATE SET paused=excluded.paused,reason=excluded.reason,
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""", (conversation_key, int(bool(paused)), reason[:200]))

    def is_paused(self, conversation_key: str) -> bool:
        with self._lock:
            row = self._connection.execute("SELECT paused FROM service_handoffs WHERE conversation_key=?", (conversation_key,)).fetchone()
        return bool(row and row[0])

    def list_handoffs(self) -> list[dict]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM service_handoffs WHERE paused=1 ORDER BY updated_at DESC").fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            try:
                platform, shop_id, conversation_id = json.loads(entry["conversation_key"])
                entry.update(platform=platform, shop_id=shop_id, conversation_id=conversation_id)
            except (ValueError, TypeError):
                pass
            result.append(entry)
        return result

    def close(self) -> None:
        with self._lock:
            self._connection.close()
