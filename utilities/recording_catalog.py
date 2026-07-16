"""Indexed catalog for finalized CCTV recording segments."""

from __future__ import annotations

import sqlite3
import threading
import time
import queue
from contextlib import contextmanager
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


@dataclass(frozen=True)
class RecordingSummary:
    count: int
    latest: Recording | None


class RecordingCatalog:
    def __init__(self, recordings_dir: Path):
        self.recordings_dir = Path(recordings_dir)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.recordings_dir / "recording_index.db"
        self._lock = threading.RLock()
        self._connection_pool: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(
            maxsize=8
        )
        self._last_reconcile = 0.0
        self._reconcile_thread: threading.Thread | None = None
        self._reconcile_stop = threading.Event()
        self._initialize()

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _connection(self):
        try:
            connection = self._connection_pool.get_nowait()
        except queue.Empty:
            connection = self._new_connection()
        reusable = True
        try:
            yield connection
            connection.commit()
        except sqlite3.DatabaseError:
            reusable = False
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            if reusable:
                try:
                    self._connection_pool.put_nowait(connection)
                except queue.Full:
                    connection.close()
            else:
                connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            try:
                connection.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                connection.execute("PRAGMA journal_mode=DELETE")
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
        with self._lock, self._connection() as connection:
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
            now = time.monotonic()
            if not force and now - self._last_reconcile < minimum_interval:
                return 0
            self._last_reconcile = now
        paths = sorted(
            path
            for path in self.recordings_dir.glob("recording_*.mp4")
            if path.is_file() and not path.name.endswith(".partial")
        )
        parsed = [(self.parse_start(path), path) for path in paths]
        parsed = [(start, path) for start, path in parsed if start is not None]
        entries = []
        for index, (start, path) in enumerate(parsed):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            end = (
                parsed[index + 1][0]
                if index + 1 < len(parsed)
                else start + timedelta(seconds=60)
            )
            entries.append((start, end, path, stat))
        existing_paths = {str(path) for _, _, path, _ in entries}

        # Keep filesystem traversal outside this lock so API summary/range reads
        # remain responsive during a large external-drive scan.
        with self._lock, self._connection() as connection:
            known = {
                row["path"]: row
                for row in connection.execute(
                    "SELECT path, size, mtime FROM recordings"
                )
            }
            changed = 0
            for start, end, path, stat in entries:
                previous = known.get(str(path))
                if (
                    previous
                    and previous["size"] == stat.st_size
                    and previous["mtime"] == stat.st_mtime
                ):
                    continue
                duration = max(0.001, (end - start).total_seconds())
                connection.execute(
                    """
                    INSERT INTO recordings(path, filename, start_time, end_time, duration, codec, size, mtime, complete)
                    VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 1)
                    ON CONFLICT(path) DO UPDATE SET end_time=excluded.end_time,
                        duration=excluded.duration, size=excluded.size, mtime=excluded.mtime, complete=1
                    """,
                    (
                        str(path),
                        path.name,
                        start.isoformat(),
                        end.isoformat(),
                        duration,
                        stat.st_size,
                        stat.st_mtime,
                    ),
                )
                changed += 1
            stale = [
                path
                for path in set(known) - existing_paths
                if not Path(path).exists()
            ]
            if stale:
                connection.executemany(
                    "DELETE FROM recordings WHERE path = ?",
                    ((path,) for path in stale),
                )
                changed += len(stale)
            return changed

    def start_background_reconcile(self, *, interval: float = 60.0) -> None:
        """Keep the fallback filesystem index fresh without blocking API requests."""
        with self._lock:
            if self._reconcile_thread and self._reconcile_thread.is_alive():
                return
            self._reconcile_stop.clear()
            self._reconcile_thread = threading.Thread(
                target=self._reconcile_loop,
                args=(max(1.0, float(interval)),),
                name="recording-catalog-reconcile",
                daemon=True,
            )
            self._reconcile_thread.start()

    def stop_background_reconcile(self) -> None:
        with self._lock:
            thread = self._reconcile_thread
            self._reconcile_thread = None
            self._reconcile_stop.set()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _reconcile_loop(self, interval: float) -> None:
        force = True
        while not self._reconcile_stop.is_set():
            try:
                self.reconcile(force=force, minimum_interval=interval)
            except (OSError, sqlite3.DatabaseError):
                # A temporarily unavailable external drive should not terminate the
                # server; retry on the next interval.
                pass
            force = False
            self._reconcile_stop.wait(interval)

    def all(
        self,
        *,
        descending: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Recording]:
        direction = "DESC" if descending else "ASC"
        with self._lock, self._connection() as connection:
            sql = f"SELECT * FROM recordings WHERE complete = 1 ORDER BY start_time {direction}"
            parameters: tuple[int, ...] = ()
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                parameters = (max(0, limit), max(0, offset))
            elif offset:
                sql += " LIMIT -1 OFFSET ?"
                parameters = (max(0, offset),)
            rows = connection.execute(sql, parameters).fetchall()
        return [self._from_row(row) for row in rows if Path(row["path"]).exists()]

    def overlapping(self, start: datetime, end: datetime) -> list[Recording]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM recordings
                WHERE complete = 1 AND start_time < ? AND end_time > ?
                ORDER BY start_time ASC
                """,
                (end.isoformat(), start.isoformat()),
            ).fetchall()
        return [self._from_row(row) for row in rows if Path(row["path"]).exists()]

    def summary(self) -> RecordingSummary:
        with self._lock, self._connection() as connection:
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM recordings WHERE complete = 1"
                ).fetchone()[0]
            )
            row = connection.execute(
                """
                SELECT * FROM recordings
                WHERE complete = 1
                ORDER BY start_time DESC
                LIMIT 1
                """
            ).fetchone()
        return RecordingSummary(count=count, latest=self._from_row(row) if row else None)

    def range_with_neighbors(
        self,
        start: datetime,
        end: datetime,
        *,
        before: int = 0,
        after: int = 0,
    ) -> list[Recording]:
        """Return a timestamp range plus a bounded number of adjacent segments."""
        start_value = start.isoformat()
        end_value = end.isoformat()
        with self._lock, self._connection() as connection:
            preceding = connection.execute(
                """
                SELECT * FROM recordings
                WHERE complete = 1 AND start_time < ?
                ORDER BY start_time DESC LIMIT ?
                """,
                (start_value, max(0, before)),
            ).fetchall()
            within = connection.execute(
                """
                SELECT * FROM recordings
                WHERE complete = 1 AND start_time >= ? AND start_time <= ?
                ORDER BY start_time ASC
                """,
                (start_value, end_value),
            ).fetchall()
            following = connection.execute(
                """
                SELECT * FROM recordings
                WHERE complete = 1 AND start_time > ?
                ORDER BY start_time ASC LIMIT ?
                """,
                (end_value, max(0, after)),
            ).fetchall()
        rows = list(reversed(preceding)) + list(within) + list(following)
        return [self._from_row(row) for row in rows if Path(row["path"]).exists()]

    def around(self, timestamp: datetime, *, before: int = 1, after: int = 1) -> list[Recording]:
        """Return the segment at/before a timestamp and its immediate neighbors."""
        value = timestamp.isoformat()
        with self._lock, self._connection() as connection:
            target = connection.execute(
                """
                SELECT * FROM recordings
                WHERE complete = 1 AND start_time <= ?
                ORDER BY start_time DESC LIMIT 1
                """,
                (value,),
            ).fetchone()
            if target is None:
                target = connection.execute(
                    """
                    SELECT * FROM recordings WHERE complete = 1
                    ORDER BY start_time ASC LIMIT 1
                    """
                ).fetchone()
            if target is None:
                return []
            previous = connection.execute(
                """
                SELECT * FROM recordings
                WHERE complete = 1 AND start_time < ?
                ORDER BY start_time DESC LIMIT ?
                """,
                (target["start_time"], max(0, before)),
            ).fetchall()
            following = connection.execute(
                """
                SELECT * FROM recordings
                WHERE complete = 1 AND start_time > ?
                ORDER BY start_time ASC LIMIT ?
                """,
                (target["start_time"], max(0, after)),
            ).fetchall()
        rows = list(reversed(previous)) + [target] + list(following)
        return [self._from_row(row) for row in rows if Path(row["path"]).exists()]

    def remove(self, paths: Iterable[Path]) -> None:
        with self._lock, self._connection() as connection:
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
