"""Persistent at-most-once delivery journal, separate from reply generation."""
from contextlib import contextmanager
import hashlib
import json
import sqlite3
from pathlib import Path


class DeliveryJournal:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS deliveries (key TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def key(event):
        identity = [event.get(k, '') for k in ('platform', 'shop_id', 'conversation_id', 'user_id', 'message_id')]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()

    def reserve(self, event, status='processing'):
        key = self.key(event)
        with self._db() as db:
            cur = db.execute('INSERT OR IGNORE INTO deliveries(key,status) VALUES(?,?)', (key, status))
            return key, cur.rowcount == 1

    def set(self, key, status):
        with self._db() as db:
            db.execute('UPDATE deliveries SET status=?, updated_at=CURRENT_TIMESTAMP WHERE key=?', (status, key))

    def status(self, event):
        with self._db() as db:
            row = db.execute('SELECT status FROM deliveries WHERE key=?', (self.key(event),)).fetchone()
            return row[0] if row else None
