"""Motion detection database module using SQLAlchemy ORM.

This module manages motion detection events in a SQLite database.
"""
from datetime import datetime, time, timedelta
from pathlib import Path
import os
from typing import Generator, Optional
from sqlalchemy import create_engine, Column, Integer, DateTime, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager

# Database path
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_DIR = REPO_ROOT / "motion" / "data"
PRIMARY_DB_DIR = Path(
    os.getenv("CCTV_RECORDINGS_DIR", "/Volumes/drive/CCTV/recordings/esp_cam1")
).expanduser()
try:
    DB_DIR = PRIMARY_DB_DIR
    DB_DIR.mkdir(parents=True, exist_ok=True)
except (PermissionError, FileNotFoundError, OSError):
    # Fallback to local data directory
    DB_DIR = DEFAULT_DB_DIR
    DB_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Warning: primary DB path unavailable, using: {DB_DIR}")

DB_PATH = DB_DIR / "motion_logs.db"

# Create engine
engine = create_engine(f'sqlite:///{DB_PATH}', echo=False)

# Base class for models
Base = declarative_base()

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class MotionEvent(Base):
    """Model for motion detection events."""
    __tablename__ = "motion_events"
    
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    
    def __repr__(self):
        return f"<MotionEvent(id={self.id}, timestamp={self.timestamp})>"
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat()
        }


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


def log_motion_event(timestamp: Optional[datetime] = None) -> MotionEvent:
    """Log a motion detection event to the database.
    
    Args:
        timestamp: Optional timestamp. If None, uses current time.
        
    Returns:
        The created MotionEvent instance
    """
    if timestamp is None:
        timestamp = datetime.now()
    
    with get_db_session() as session:
        event = MotionEvent(timestamp=timestamp)
        session.add(event)
        session.flush()
        session.refresh(event)
        return event


def get_motion_events_by_hours(hours: int) -> list[MotionEvent]:
    """Get motion events from the last N hours.
    
    Args:
        hours: Number of hours to look back
        
    Returns:
        List of MotionEvent instances
    """
    from datetime import timedelta
    
    start_time = datetime.now() - timedelta(hours=hours)
    
    with get_db_session() as session:
        events = session.query(MotionEvent).filter(
            MotionEvent.timestamp >= start_time
        ).order_by(MotionEvent.timestamp.desc()).all()
        
        # Detach from session
        return [MotionEvent(id=e.id, timestamp=e.timestamp) for e in events]

def get_motion_events_daytime(date: datetime) -> list[MotionEvent]:
    """Get motion events between 7:00 AM and 11:00 PM on a given date."""
    
    start_time = datetime.combine(date.date(), time(7, 0))
    end_time = datetime.combine(date.date(), time(23, 0))

    with get_db_session() as session:
        events = (
            session.query(MotionEvent)
            .filter(
                MotionEvent.timestamp >= start_time,
                MotionEvent.timestamp <= end_time,
            )
            .order_by(MotionEvent.timestamp.asc())
            .all()
        )

        return [MotionEvent(id=e.id, timestamp=e.timestamp) for e in events]

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
    
    with get_db_session() as session:
        events = session.query(MotionEvent).filter(
            MotionEvent.timestamp >= start_time,
            MotionEvent.timestamp < end_time
        ).order_by(MotionEvent.timestamp.asc()).all()
        
        # Detach from session
        return [MotionEvent(id=e.id, timestamp=e.timestamp) for e in events]


def get_motion_events_by_range(start: datetime, end: datetime) -> list[MotionEvent]:
    """Get motion events within a time range.
    
    Args:
        start: Start timestamp
        end: End timestamp
        
    Returns:
        List of MotionEvent instances
    """
    with get_db_session() as session:
        events = session.query(MotionEvent).filter(
            MotionEvent.timestamp >= start,
            MotionEvent.timestamp <= end
        ).order_by(MotionEvent.timestamp.asc()).all()
        
        # Detach from session
        return [MotionEvent(id=e.id, timestamp=e.timestamp) for e in events]


def get_total_motion_count() -> int:
    """Get total count of motion events."""
    with get_db_session() as session:
        return session.query(MotionEvent).count()


def get_motion_event_stats_per_hour(last_days: int = 30) -> list[dict]:
    """Get motion event counts by hour-of-day (00-23) for the last N days."""
    end_time = datetime.now()
    start_time = end_time - timedelta(days=last_days)

    with get_db_session() as session:
        rows = (
            session.query(
                func.strftime("%H", MotionEvent.timestamp).label("hour"),
                func.count(MotionEvent.id).label("count"),
            )
            .filter(
                MotionEvent.timestamp >= start_time,
                MotionEvent.timestamp <= end_time,
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

    with get_db_session() as session:
        rows = (
            session.query(
                func.strftime("%Y-%m-%d %H:00:00", MotionEvent.timestamp).label("bucket"),
                func.count(MotionEvent.id).label("count"),
            )
            .filter(
                MotionEvent.timestamp >= start_time,
                MotionEvent.timestamp <= end_time,
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
    with get_db_session() as session:
        min_ts, max_ts = session.query(
            func.min(MotionEvent.timestamp),
            func.max(MotionEvent.timestamp),
        ).one()

        if not min_ts or not max_ts:
            return [
                {"hour": f"{hour:02d}:00", "avg_per_day": 0.0, "total_events": 0, "days": 0}
                for hour in range(24)
            ]

        rows = (
            session.query(
                func.strftime("%H", MotionEvent.timestamp).label("hour"),
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
