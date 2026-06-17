from tools.adjustLED import adJustLED
import time
import threading


class NonBlockingBlinker:
    """A non-blocking LED blinker that updates based on time checks without sleep.

    LED control requests are dispatched in fire-and-forget background threads
    so the main frame loop is never blocked, and a guard prevents piling up
    concurrent HTTP requests to the ESP32.
    """

    def __init__(self, blink_interval=0.5):
        self.blink_interval = blink_interval
        self.last_toggle_time = 0
        self.led_state = False
        self.is_active = False
        self.start_time = 0
        self.duration = 0
        self._led_busy = threading.Lock()

    def _set_led(self, brightness: int) -> None:
        """Send LED command in a background thread (fire-and-forget).

        A threading lock ensures at most one request is in-flight at a time;
        if a previous request is still running the new one is silently dropped.
        """
        if not self._led_busy.acquire(blocking=False):
            return  # previous request still in-flight, skip

        def _do():
            try:
                adJustLED(brightness)
            finally:
                self._led_busy.release()

        threading.Thread(target=_do, daemon=True).start()

    def start(self, duration=5):
        """Start the blink sequence"""
        self.is_active = True
        self.start_time = time.time()
        self.duration = duration
        self.last_toggle_time = time.time()
        self.led_state = False

    def update(self):
        """Call this every frame to update the LED state without blocking"""
        if not self.is_active:
            return

        current_time = time.time()

        # Check if duration has elapsed
        if current_time - self.start_time >= self.duration:
            self.is_active = False
            self._set_led(0)
            self.led_state = False
            return

        # Toggle LED at intervals
        if current_time - self.last_toggle_time >= self.blink_interval:
            self.led_state = not self.led_state
            self._set_led(10 if self.led_state else 0)
            self.last_toggle_time = current_time

    def stop(self):
        """Stop the blink sequence"""
        self.is_active = False
        self._set_led(0)
        self.led_state = False


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
