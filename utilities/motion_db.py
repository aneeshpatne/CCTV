"""Motion detection database module using SQLAlchemy ORM.

This module manages motion detection events in a SQLite database.
"""

from datetime import datetime
from pathlib import Path
from typing import Generator
from sqlalchemy import create_engine, Column, Integer, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager

# Database path
try:
    DB_DIR = Path("/Volumes/drive/CCTV/recordings/esp_cam1")
    DB_DIR.mkdir(parents=True, exist_ok=True)
except (PermissionError, FileNotFoundError, OSError):
    # Fallback to local data directory
    DB_DIR = Path(__file__).parent.parent / "motion" / "data"
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


def log_motion_event(timestamp: datetime | None = None) -> MotionEvent:
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
