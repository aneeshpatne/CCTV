"""Motion detection database module using SQLAlchemy ORM.

This module manages motion detection events in a SQLite database.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path
import os
from typing import Generator, Optional
from sqlalchemy import create_engine, Column, Integer, DateTime, Float, String, Text, ForeignKey, case, func, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import DatabaseError
from contextlib import contextmanager

def _resolve_db_dir() -> Path:
    candidates = [
        os.getenv("MOTION_DB_DIR"),
        os.getenv("CCTV_RECORDINGS_DIR"),
        os.getenv("MOTION_DATA_DIR"),
        os.getenv("DATA_DIR"),
        "/Volumes/HP USB20FD/CCTV/recordings/esp_cam1",
        "/Volumes/HP USB20FD/CCTV/motion/data",
    ]

    for candidate in candidates:
        if not candidate:
            continue
        try:
            path = Path(candidate).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            return path
        except (PermissionError, FileNotFoundError, OSError):
            continue

    raise RuntimeError(
        "No valid SSD-backed motion DB directory found. "
        "Set MOTION_DB_DIR or CCTV_RECORDINGS_DIR to a mounted writable volume path."
    )


DB_DIR = _resolve_db_dir()

DB_PATH = DB_DIR / "motion_logs.db"

# Create engine
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)

# Base class for models
Base = declarative_base()

# Session factory
SessionLocal = sessionmaker(
    autocommit=False, autoflush=False, expire_on_commit=False, bind=engine
)


class MotionEvent(Base):
    """Model for motion detection events."""

    __tablename__ = "motion_events_new"

    id = Column(Integer, primary_key=True, index=True)
    start_time = Column(DateTime, nullable=False, index=True)
    end_time = Column(DateTime, nullable=False, index=True)
    duration = Column(Float, nullable=False)

    def __repr__(self):
        return (
            f"<MotionEvent(id={self.id}, start_time={self.start_time}, "
            f"end_time={self.end_time}, duration={self.duration})>"
        )

    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "duration": self.duration,
        }


class MotionEventAnnotation(Base):
    """Additive native-detector metadata; the existing event table stays unchanged."""

    __tablename__ = "motion_event_annotations"

    event_id = Column(
        Integer,
        ForeignKey("motion_events_new.id", ondelete="CASCADE"),
        primary_key=True,
    )
    detector_version = Column(String(64), nullable=False)
    confidence = Column(Float, nullable=False, default=0.0)
    labels_json = Column(Text, nullable=False, default="[]")


# Create tables
Base.metadata.create_all(bind=engine)


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """Context manager for database sessions."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def get_read_session() -> Generator[Session, None, None]:
    """Read-only sessions do not acquire a needless SQLite write transaction."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def log_motion_event(
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    duration: Optional[float] = None,
) -> MotionEvent:
    """Log a motion detection event to the database.

    Args:
        start_time: Event start timestamp. Defaults to current time.
        end_time: Event end timestamp. If omitted, will be derived.
        duration: Event duration in seconds. If omitted, will be derived.

    Returns:
        The created MotionEvent instance
    """
    if start_time is None:
        start_time = datetime.now()

    if end_time is None and duration is not None:
        end_time = start_time + timedelta(seconds=float(duration))
    if end_time is None:
        end_time = start_time

    if duration is None:
        duration = (end_time - start_time).total_seconds()

    with get_db_session() as session:
        event = MotionEvent(
            start_time=start_time,
            end_time=end_time,
            duration=float(duration),
        )
        session.add(event)
        session.flush()
        session.refresh(event)
        return event


def annotate_motion_event(
    event_id: int,
    *,
    detector_version: str,
    confidence: float,
    labels_json: str,
) -> None:
    """Insert or update optional metadata for an existing motion event."""
    with get_db_session() as session:
        annotation = session.get(MotionEventAnnotation, int(event_id))
        if annotation is None:
            annotation = MotionEventAnnotation(event_id=int(event_id))
            session.add(annotation)
        annotation.detector_version = detector_version
        annotation.confidence = float(confidence)
        annotation.labels_json = labels_json


def get_motion_annotations(event_ids: list[int]) -> dict[int, dict]:
    if not event_ids:
        return {}
    import json

    with get_read_session() as session:
        annotations = (
            session.query(MotionEventAnnotation)
            .filter(MotionEventAnnotation.event_id.in_(event_ids))
            .all()
        )
        result = {}
        for annotation in annotations:
            try:
                labels = json.loads(annotation.labels_json)
            except (TypeError, ValueError):
                labels = []
            result[int(annotation.event_id)] = {
                "labels": labels,
                "confidence": float(annotation.confidence),
                "detector_version": annotation.detector_version,
            }
        return result


def _coerce_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _scan_events_without_indexes(
    where_sql: str, params: dict[str, object], order_direction: str
) -> list[MotionEvent]:
    sql = text(
        "SELECT id, start_time, end_time, duration "
        "FROM motion_events_new NOT INDEXED "
        f"WHERE {where_sql} "
        f"ORDER BY start_time {order_direction}"
    )

    with engine.connect() as conn:
        rows = conn.execute(sql, params).mappings().all()

    return [
        MotionEvent(
            id=row["id"],
            start_time=_coerce_dt(row["start_time"]),
            end_time=_coerce_dt(row["end_time"]),
            duration=float(row["duration"]),
        )
        for row in rows
    ]


def _read_events_with_fallback(
    session: Session,
    *,
    filters: tuple,
    where_sql: str,
    params: dict[str, object],
    ascending: bool,
) -> list[MotionEvent]:
    order_by = (
        MotionEvent.start_time.asc() if ascending else MotionEvent.start_time.desc()
    )
    try:
        return session.query(MotionEvent).filter(*filters).order_by(order_by).all()
    except DatabaseError as exc:
        if "database disk image is malformed" not in str(exc).lower():
            raise
        return _scan_events_without_indexes(
            where_sql=where_sql,
            params=params,
            order_direction="ASC" if ascending else "DESC",
        )


def get_motion_events_by_hours(hours: int) -> list[MotionEvent]:
    """Get motion events from the last N hours.

    Args:
        hours: Number of hours to look back

    Returns:
        List of MotionEvent instances
    """
    from datetime import timedelta

    start_time = datetime.now() - timedelta(hours=hours)

    with get_read_session() as session:
        return _read_events_with_fallback(
            session,
            filters=(MotionEvent.start_time >= start_time,),
            where_sql="start_time >= :start_time",
            params={"start_time": start_time},
            ascending=False,
        )


def get_motion_events_daytime(date: datetime) -> list[MotionEvent]:
    """Get motion events between 7:00 AM and 11:00 PM on a given date."""

    start_time = datetime.combine(date.date(), time(7, 0))
    end_time = datetime.combine(date.date(), time(23, 0))

    with get_read_session() as session:
        return _read_events_with_fallback(
            session,
            filters=(
                MotionEvent.start_time >= start_time,
                MotionEvent.start_time <= end_time,
            ),
            where_sql="start_time >= :start_time AND start_time <= :end_time",
            params={"start_time": start_time, "end_time": end_time},
            ascending=True,
        )


def get_motion_events_by_date(date: datetime) -> list[MotionEvent]:
    """Get motion events for a specific date.

    Args:
        date: Date to query (time component will be ignored)

    Returns:
        List of MotionEvent instances
    """
    from datetime import timedelta

    start_time = date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_time = start_time + timedelta(days=1)

    with get_read_session() as session:
        return _read_events_with_fallback(
            session,
            filters=(
                MotionEvent.start_time >= start_time,
                MotionEvent.start_time < end_time,
            ),
            where_sql="start_time >= :start_time AND start_time < :end_time",
            params={"start_time": start_time, "end_time": end_time},
            ascending=True,
        )


def get_motion_events_by_range(start: datetime, end: datetime) -> list[MotionEvent]:
    """Get motion events within a time range.

    Args:
        start: Start timestamp
        end: End timestamp

    Returns:
        List of MotionEvent instances
    """
    with get_read_session() as session:
        return _read_events_with_fallback(
            session,
            filters=(MotionEvent.start_time >= start, MotionEvent.start_time <= end),
            where_sql="start_time >= :start_time AND start_time <= :end_time",
            params={"start_time": start, "end_time": end},
            ascending=True,
        )


def get_total_motion_count() -> int:
    """Get total count of motion events."""
    with get_read_session() as session:
        return session.query(MotionEvent).count()


def get_motion_counts() -> dict[str, int]:
    """Count all dashboard windows in one indexed aggregate query."""
    now = datetime.now()
    hour_1 = now - timedelta(hours=1)
    hour_12 = now - timedelta(hours=12)
    hour_24 = now - timedelta(hours=24)
    with get_read_session() as session:
        row = session.query(
            func.count(MotionEvent.id),
            func.sum(case((MotionEvent.start_time >= hour_1, 1), else_=0)),
            func.sum(case((MotionEvent.start_time >= hour_12, 1), else_=0)),
            func.sum(case((MotionEvent.start_time >= hour_24, 1), else_=0)),
        ).one()
    return {
        "total_events": int(row[0] or 0),
        "last_hour": int(row[1] or 0),
        "last_12_hours": int(row[2] or 0),
        "last_24_hours": int(row[3] or 0),
    }


def get_motion_event_stats_per_hour(last_days: int = 30) -> list[dict]:
    """Get motion event counts by hour-of-day (00-23) for the last N days."""
    end_time = datetime.now()
    start_time = end_time - timedelta(days=last_days)

    with get_read_session() as session:
        rows = (
            session.query(
                func.strftime("%H", MotionEvent.start_time).label("hour"),
                func.count(MotionEvent.id).label("count"),
            )
            .filter(
                MotionEvent.start_time >= start_time,
                MotionEvent.start_time <= end_time,
            )
            .group_by("hour")
            .all()
        )

    counts_by_hour = {int(row.hour): int(row.count) for row in rows}
    return [
        {"hour": f"{hour:02d}:00", "count": counts_by_hour.get(hour, 0)}
        for hour in range(24)
    ]


def get_motion_event_stats_per_hour_last_month() -> list[dict]:
    """Get motion event counts for each hourly bucket over the last 30 days."""
    end_time = datetime.now()
    start_time = end_time - timedelta(days=30)

    with get_read_session() as session:
        rows = (
            session.query(
                func.strftime("%Y-%m-%d %H:00:00", MotionEvent.start_time).label(
                    "bucket"
                ),
                func.count(MotionEvent.id).label("count"),
            )
            .filter(
                MotionEvent.start_time >= start_time,
                MotionEvent.start_time <= end_time,
            )
            .group_by("bucket")
            .all()
        )

    counts_by_bucket = {row.bucket: int(row.count) for row in rows}
    current = start_time.replace(minute=0, second=0, microsecond=0)
    last_bucket = end_time.replace(minute=0, second=0, microsecond=0)
    buckets: list[dict] = []

    while current <= last_bucket:
        bucket_key = current.strftime("%Y-%m-%d %H:00:00")
        buckets.append(
            {
                "hour": bucket_key,
                "count": counts_by_bucket.get(bucket_key, 0),
            }
        )
        current += timedelta(hours=1)

    return buckets


def get_motion_event_hourly_avg_all_time() -> list[dict]:
    """Get average events per day for each hour (00-23) across all stored dates."""
    with get_read_session() as session:
        min_ts, max_ts = session.query(
            func.min(MotionEvent.start_time),
            func.max(MotionEvent.start_time),
        ).one()

        if not min_ts or not max_ts:
            return [
                {
                    "hour": f"{hour:02d}:00",
                    "avg_per_day": 0.0,
                    "total_events": 0,
                    "days": 0,
                }
                for hour in range(24)
            ]

        rows = (
            session.query(
                func.strftime("%H", MotionEvent.start_time).label("hour"),
                func.count(MotionEvent.id).label("count"),
            )
            .group_by("hour")
            .all()
        )

    total_days = (max_ts.date() - min_ts.date()).days + 1
    counts_by_hour = {int(row.hour): int(row.count) for row in rows}

    return [
        {
            "hour": f"{hour:02d}:00",
            "avg_per_day": round(counts_by_hour.get(hour, 0) / total_days, 3),
            "total_events": counts_by_hour.get(hour, 0),
            "days": total_days,
        }
        for hour in range(24)
    ]
