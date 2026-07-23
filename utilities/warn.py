from tools.adjustLED import adJustLED
import time
import threading


class NonBlockingBlinker:
    """Run one guarded quick-double-flash sequence without blocking frames."""

    _pattern = ((10, 0.2), (0, 0.2), (10, 0.2), (0, 1.0))

    def __init__(self, blink_interval=0.5, dispatcher=None):
        # blink_interval remains accepted for compatibility with older callers.
        self.blink_interval = blink_interval
        self.led_state = False
        self.is_active = False
        self.start_time = 0
        self.duration = 0
        self._phase = 0
        self._next_transition = 0
        self._dispatcher = dispatcher or _SerialLEDDispatcher(adJustLED)

    def _set_led(self, brightness: int) -> None:
        self._dispatcher.submit(brightness)

    def start(self, duration=30, now=None):
        """Start immediately, coalescing duplicate starts while active."""
        if self.is_active:
            return False
        current_time = time.monotonic() if now is None else now
        self.is_active = True
        self.start_time = current_time
        self.duration = duration
        self._phase = 0
        self._next_transition = current_time + self._pattern[0][1]
        self.led_state = True
        self._set_led(self._pattern[0][0])
        return True

    def update(self, now=None):
        """Call this every frame to update the LED state without blocking"""
        if not self.is_active:
            return

        current_time = time.monotonic() if now is None else now

        if current_time - self.start_time >= self.duration:
            self.stop()
            return

        advanced = False
        while current_time >= self._next_transition:
            self._phase = (self._phase + 1) % len(self._pattern)
            self._next_transition += self._pattern[self._phase][1]
            advanced = True
        if advanced:
            brightness = self._pattern[self._phase][0]
            self.led_state = brightness > 0
            self._set_led(brightness)

    def stop(self):
        """Stop the blink sequence"""
        self.is_active = False
        self._set_led(0)
        self.led_state = False

    def close(self):
        """Leave the LED off and stop the serial dispatcher when owned here."""
        self.stop()
        close = getattr(self._dispatcher, "close", None)
        if close is not None:
            close()


class _SerialLEDDispatcher:
    """Allow one HTTP request in flight and coalesce queued LED states."""

    def __init__(self, setter):
        self._setter = setter
        self._condition = threading.Condition()
        self._pending = None
        self._closed = False
        self._worker = threading.Thread(
            target=self._run,
            daemon=True,
            name="camera-led-dispatcher",
        )
        self._worker.start()

    def submit(self, brightness: int) -> None:
        with self._condition:
            if self._closed:
                return
            self._pending = brightness
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout=1)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._closed and self._pending is None:
                    self._condition.wait()
                if self._closed and self._pending is None:
                    return
                brightness = self._pending
                self._pending = None
            self._setter(brightness)


def warning_blink(duration=5, interval=0.5):
    start_time = time.time()
    while time.time() - start_time < duration:
        adJustLED(10)
        time.sleep(interval)
        adJustLED(0)
        time.sleep(interval)
    adJustLED(0)
    print("⚠️ Warning blink complete.")


def warning_blink_alternate(duration=5, interval=0.5):
    start_time = time.time()
    while time.time() - start_time < duration:
        # Pattern: quick double flash
        adJustLED(10)
        time.sleep(0.2)
        adJustLED(0)
        time.sleep(0.2)
        adJustLED(10)
        time.sleep(0.2)
        adJustLED(0)
        time.sleep(1.0)  # Longer pause
    adJustLED(0)
    print("⚠️ Alternate warning blink complete.")


def warning_smooth_glow(duration=5, step=0.1):
    start_time = time.time()
    while time.time() - start_time < duration:
        # Fade in
        for brightness in range(0, 10, 1):
            adJustLED(brightness)
            time.sleep(step)
        # Fade out
        for brightness in range(10, -1, -1):
            adJustLED(brightness)
            time.sleep(step)
    adJustLED(0)
    print("⚠️ Smooth glow complete.")
