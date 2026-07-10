"""Indexed catalog for finalized CCTV recording segments."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

FILENAME_FORMAT = "recording_%Y%m%d_%H%M%S.mp4"


@dataclass(frozen=True)
class Recording:
    path: Path
    start_time: datetime
    end_time: datetime
    duration: float
    codec: str | None
    size: int


class RecordingCatalog:
    def __init__(self, recordings_dir: Path):
        self.recordings_dir = Path(recordings_dir)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.recordings_dir / "recording_index.db"
        self._lock = threading.RLock()
        self._last_reconcile = 0.0
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            connection.execute("PRAGMA journal_mode=DELETE")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS recordings (
                    path TEXT PRIMARY KEY,
                    filename TEXT NOT NULL UNIQUE,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    duration REAL NOT NULL,
                    codec TEXT,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    complete INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS ix_recordings_start ON recordings(start_time);
                CREATE INDEX IF NOT EXISTS ix_recordings_end ON recordings(end_time);
                """
            )

    @staticmethod
    def parse_start(path: Path) -> datetime | None:
        try:
            return datetime.strptime(path.name, FILENAME_FORMAT)
        except ValueError:
            return None

    def register(
        self,
        path: Path,
        start_time: datetime,
        end_time: datetime,
        *,
        codec: str | None = None,
        size: int | None = None,
    ) -> None:
        path = Path(path)
        if path.suffix != ".mp4" or path.name.endswith(".partial"):
            return
        stat = path.stat()
        duration = max(0.001, (end_time - start_time).total_seconds())
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO recordings(path, filename, start_time, end_time, duration, codec, size, mtime, complete)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(path) DO UPDATE SET
                    filename=excluded.filename,
                    start_time=excluded.start_time,
                    end_time=excluded.end_time,
                    duration=excluded.duration,
                    codec=COALESCE(excluded.codec, recordings.codec),
                    size=excluded.size,
                    mtime=excluded.mtime,
                    complete=1
                """,
                (
                    str(path),
                    path.name,
                    start_time.isoformat(),
                    end_time.isoformat(),
                    duration,
                    codec,
                    int(size if size is not None else stat.st_size),
                    stat.st_mtime,
                ),
            )

    def reconcile(self, *, force: bool = False, minimum_interval: float = 60.0) -> int:
        now = time.monotonic()
        if not force and now - self._last_reconcile < minimum_interval:
            return 0
        with self._lock:
            self._last_reconcile = now
            paths = sorted(
                path
                for path in self.recordings_dir.glob("recording_*.mp4")
                if path.is_file() and not path.name.endswith(".partial")
            )
            parsed = [(self.parse_start(path), path) for path in paths]
            parsed = [(start, path) for start, path in parsed if start is not None]
            existing_paths = {str(path) for _, path in parsed}
            with self._connect() as connection:
                known = {
                    row["path"]: row
                    for row in connection.execute("SELECT path, size, mtime FROM recordings")
                }
                changed = 0
                for index, (start, path) in enumerate(parsed):
                    stat = path.stat()
                    previous = known.get(str(path))
                    if previous and previous["size"] == stat.st_size and previous["mtime"] == stat.st_mtime:
                        continue
                    end = parsed[index + 1][0] if index + 1 < len(parsed) else start + timedelta(seconds=60)
                    duration = max(0.001, (end - start).total_seconds())
                    connection.execute(
                        """
                        INSERT INTO recordings(path, filename, start_time, end_time, duration, codec, size, mtime, complete)
                        VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 1)
                        ON CONFLICT(path) DO UPDATE SET end_time=excluded.end_time,
                            duration=excluded.duration, size=excluded.size, mtime=excluded.mtime, complete=1
                        """,
                        (str(path), path.name, start.isoformat(), end.isoformat(), duration, stat.st_size, stat.st_mtime),
                    )
                    changed += 1
                stale = set(known) - existing_paths
                if stale:
                    connection.executemany("DELETE FROM recordings WHERE path = ?", ((path,) for path in stale))
                    changed += len(stale)
                return changed

    def all(self, *, descending: bool = False) -> list[Recording]:
        self.reconcile()
        direction = "DESC" if descending else "ASC"
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM recordings WHERE complete = 1 ORDER BY start_time {direction}"
            ).fetchall()
        return [self._from_row(row) for row in rows if Path(row["path"]).exists()]

    def overlapping(self, start: datetime, end: datetime) -> list[Recording]:
        self.reconcile()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM recordings
                WHERE complete = 1 AND start_time < ? AND end_time > ?
                ORDER BY start_time ASC
                """,
                (end.isoformat(), start.isoformat()),
            ).fetchall()
        return [self._from_row(row) for row in rows if Path(row["path"]).exists()]

    def remove(self, paths: Iterable[Path]) -> None:
        with self._lock, self._connect() as connection:
            connection.executemany("DELETE FROM recordings WHERE path = ?", ((str(path),) for path in paths))

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Recording:
        return Recording(
            path=Path(row["path"]),
            start_time=datetime.fromisoformat(row["start_time"]),
            end_time=datetime.fromisoformat(row["end_time"]),
            duration=float(row["duration"]),
            codec=row["codec"],
            size=int(row["size"]),
        )
