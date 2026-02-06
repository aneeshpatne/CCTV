import time
import threading
from typing import Callable, Dict, Optional


class EventAccumulator:
    def __init__(
        self,
        cooldown: float = 15.0,
        onSave: Optional[Callable[[Dict[str, float]], None]] = None,
    ):
        self.cooldown: float = float(cooldown)
        self.onSave: Callable[[Dict[str, float]], None] = onSave or self._default_save
        self._start_time: Optional[float] = None
        self._end_time: Optional[float] = None
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def trigger(self):
        now = time.time()
        with self._lock:
            if self._start_time is None:
                self._start_time = now
            self._end_time = now + self.cooldown

            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.cooldown, self._save_event)
            self._timer.start()

    def _save_event(self):
        with self._lock:
            if self._start_time is None or self._end_time is None:
                return
            event = {
                "start_time": self._start_time,
                "end_time": self._end_time,
                "duration": self._end_time - self._start_time,
            }
            self._start_time = None
            self._end_time = None
            self._timer = None
        self.onSave(event)

    def _default_save(self, event):
        print(f"Event saved: duration={event['duration']:.2f}s")
