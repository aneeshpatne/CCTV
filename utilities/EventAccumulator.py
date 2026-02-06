import logging
import time
import threading
from typing import Callable, Dict, Optional
from datetime import timedelta


class EventAccumulator:
    def __init__(
        self,
        cooldown: float = 15.0,
        onSave: Optional[Callable[[Dict[str, float]], None]] = None,
    ):
        self._logger = logging.getLogger(__name__)
        self.cooldown: float = float(cooldown)
        self.onSave: Callable[[Dict[str, float]], None] = onSave or self._default_save
        self._start_time: Optional[float] = None
        self._end_time: Optional[float] = None
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def trigger(self):
        now = time.time()
        with self._lock:
            is_new_event = self._start_time is None
            if self._start_time is None:
                self._start_time = now
            self._end_time = now + self.cooldown

            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.cooldown, self._save_event)
            self._timer.start()
        if is_new_event:
            self._logger.info("EventAccumulator started a new motion event.")

    def _save_event(self):
        with self._lock:
            if self._start_time is None or self._end_time is None:
                return
            self._start_time -= 15.0
            event = {
                "start_time": self._start_time,
                "end_time": self._end_time,
                "duration": self._end_time - self._start_time,
            }
            self._start_time = None
            self._end_time = None
            self._timer = None
        self._logger.info(
            "EventAccumulator finalized motion event: duration=%.2fs",
            event["duration"],
        )
        self.onSave(event)

    def _default_save(self, event):
        self._logger.info("Event saved: duration=%.2fs", event["duration"])
        print(f"Event saved: duration={event['duration']:.2f}s")
