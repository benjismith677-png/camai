"""SQLite event database with thread-safe access."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Optional

from sentinel.models import Event


class EventDB:
    """Thread-safe SQLite database for events, tracks, and statistics."""

    def __init__(self, db_path: str = "data/events.db"):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            c = self.conn.cursor()
            c.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL,
                    channel INTEGER NOT NULL,
                    zone_name TEXT NOT NULL,
                    alert_level TEXT NOT NULL,
                    class_name TEXT NOT NULL,
                    track_id INTEGER,
                    confidence REAL,
                    behavior TEXT,
                    snapshot_path TEXT,
                    clip_path TEXT,
                    duration REAL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_events_channel ON events(channel);
                CREATE INDEX IF NOT EXISTS idx_events_level ON events(alert_level);
                CREATE INDEX IF NOT EXISTS idx_events_class ON events(class_name);

                CREATE TABLE IF NOT EXISTS scene_hourly (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hour TEXT NOT NULL,
                    channel INTEGER NOT NULL,
                    person_count INTEGER DEFAULT 0,
                    vehicle_count INTEGER DEFAULT 0,
                    animal_count INTEGER DEFAULT 0,
                    alert_count INTEGER DEFAULT 0,
                    UNIQUE(hour, channel)
                );

                CREATE TABLE IF NOT EXISTS alert_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    alert_level TEXT NOT NULL,
                    channel INTEGER NOT NULL,
                    message TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_alert_log_ts ON alert_log(timestamp DESC);
            """)
            self.conn.commit()

    def insert_event(self, event: Event) -> str:
        """Insert an event record. Returns event ID."""
        with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO events
                   (id, timestamp, channel, zone_name, alert_level, class_name,
                    track_id, confidence, behavior, snapshot_path, clip_path, duration)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.id, event.timestamp, event.channel, event.zone_name,
                    event.alert_level, event.class_name, event.track_id,
                    event.confidence, event.behavior, event.snapshot_path,
                    event.clip_path, event.duration,
                ),
            )
            self.conn.commit()

        # Update hourly stats
        self._update_hourly(event)
        return event.id

    def _update_hourly(self, event: Event) -> None:
        """Update scene_hourly aggregate table."""
        from datetime import datetime
        hour_str = datetime.fromtimestamp(event.timestamp).strftime("%Y-%m-%d %H:00")

        vehicle_classes = {"car", "truck", "bus", "motorcycle", "bicycle"}
        animal_classes = {"cat", "dog", "bird"}

        person_inc = 1 if event.class_name == "person" else 0
        vehicle_inc = 1 if event.class_name in vehicle_classes else 0
        animal_inc = 1 if event.class_name in animal_classes else 0
        alert_inc = 1 if event.alert_level in ("warning", "critical") else 0

        with self._lock:
            self.conn.execute(
                """INSERT INTO scene_hourly (hour, channel, person_count, vehicle_count, animal_count, alert_count)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(hour, channel) DO UPDATE SET
                   person_count = person_count + ?,
                   vehicle_count = vehicle_count + ?,
                   animal_count = animal_count + ?,
                   alert_count = alert_count + ?""",
                (hour_str, event.channel, person_inc, vehicle_inc, animal_inc, alert_inc,
                 person_inc, vehicle_inc, animal_inc, alert_inc),
            )
            self.conn.commit()

    def query_events(
        self,
        camera: Optional[int] = None,
        level: Optional[str] = None,
        class_name: Optional[str] = None,
        zone: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Query events with optional filters."""
        conditions: list[str] = []
        params: list = []

        if camera is not None:
            conditions.append("channel = ?")
            params.append(camera)
        if level:
            conditions.append("alert_level = ?")
            params.append(level)
        if class_name:
            conditions.append("class_name = ?")
            params.append(class_name)
        if zone:
            conditions.append("zone_name = ?")
            params.append(zone)
        if since:
            conditions.append("timestamp >= ?")
            params.append(since)
        if until:
            conditions.append("timestamp <= ?")
            params.append(until)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        query = f"SELECT * FROM events {where} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            rows = self.conn.execute(query, params).fetchall()

        return [dict(row) for row in rows]

    def update_clip_path(self, event_id: str, clip_path: str) -> None:
        """Update an event's clip path after clip is saved."""
        with self._lock:
            self.conn.execute(
                "UPDATE events SET clip_path = ? WHERE id = ?",
                (clip_path, event_id),
            )
            self.conn.commit()

    def get_event(self, event_id: str) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_stats(self) -> dict:
        """Return aggregate statistics."""
        with self._lock:
            total = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            by_level = self.conn.execute(
                "SELECT alert_level, COUNT(*) FROM events GROUP BY alert_level"
            ).fetchall()
            by_class = self.conn.execute(
                "SELECT class_name, COUNT(*) FROM events GROUP BY class_name ORDER BY COUNT(*) DESC LIMIT 10"
            ).fetchall()
            recent_hour = self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE timestamp > ?",
                (time.time() - 3600,)
            ).fetchone()[0]

        return {
            "total_events": total,
            "by_level": {row[0]: row[1] for row in by_level},
            "by_class": {row[0]: row[1] for row in by_class},
            "last_hour": recent_hour,
        }

    def get_hourly_stats(self, hours: int = 24) -> list[dict]:
        """Get hourly aggregates for the last N hours."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM scene_hourly
                   ORDER BY hour DESC LIMIT ?""",
                (hours,)
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self.conn.close()
