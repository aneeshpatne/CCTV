import logging
import time
import threading
from typing import Callable, Dict, Optional


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
        self._closed = False
        self._condition = threading.Condition()
        self._worker = threading.Thread(
            target=self._run,
            name="motion-event-accumulator",
            daemon=True,
        )
        self._worker.start()

    def trigger(self):
        now = time.time()
        with self._condition:
            is_new_event = self._start_time is None
            if self._start_time is None:
                self._start_time = now
            self._end_time = now + self.cooldown
            self._condition.notify()
        if is_new_event:
            self._logger.info("EventAccumulator started a new motion event.")

    def _take_event_locked(self) -> Optional[Dict[str, float]]:
        if self._start_time is None or self._end_time is None:
            return None
        padded_start = self._start_time - 15.0
        event = {
            "start_time": padded_start,
            "end_time": self._end_time,
            "duration": self._end_time - padded_start,
        }
        self._start_time = None
        self._end_time = None
        return event

    def _save_event(self) -> None:
        with self._condition:
            event = self._take_event_locked()
        self._emit_event(event)

    def _emit_event(self, event: Optional[Dict[str, float]]) -> None:
        if event is None:
            return
        self._logger.info(
            "EventAccumulator finalized motion event: duration=%.2fs",
            event["duration"],
        )
        self.onSave(event)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._closed and self._end_time is None:
                    self._condition.wait()
                if self._closed:
                    return
                remaining = self._end_time - time.time()
                if remaining > 0:
                    self._condition.wait(timeout=remaining)
                    continue
                event = self._take_event_locked()
            self._emit_event(event)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout=1)

    def _default_save(self, event):
        self._logger.info("Event saved: duration=%.2fs", event["duration"])
        print(f"Event saved: duration={event['duration']:.2f}s")
