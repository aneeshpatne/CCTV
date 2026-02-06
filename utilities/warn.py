from tools.adjustLED import adJustLED
import time

class NonBlockingBlinker:
    """A non-blocking LED blinker that updates based on time checks without sleep"""
    def __init__(self, blink_interval=0.5):
        self.blink_interval = blink_interval
        self.last_toggle_time = 0
        self.led_state = False
        self.is_active = False
        self.start_time = 0
        self.duration = 0
    
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
            adJustLED(0)
            self.led_state = False
            return
        
        # Toggle LED at intervals
        if current_time - self.last_toggle_time >= self.blink_interval:
            self.led_state = not self.led_state
            adJustLED(50 if self.led_state else 0)
            self.last_toggle_time = current_time
    
    def stop(self):
        """Stop the blink sequence"""
        self.is_active = False
        adJustLED(0)
        self.led_state = False

def warning_blink(duration=5, interval=0.5):
    start_time = time.time()
    while time.time() - start_time < duration:
        adJustLED(50)  
        time.sleep(interval)
        adJustLED(0)     
        time.sleep(interval)
    adJustLED(0)
    print("⚠️ Warning blink complete.")


def warning_blink_alternate(duration=5, interval=0.5):
    start_time = time.time()
    while time.time() - start_time < duration:
        # Pattern: quick double flash
        adJustLED(50)
        time.sleep(0.2)
        adJustLED(0)
        time.sleep(0.2)
        adJustLED(50)
        time.sleep(0.2)
        adJustLED(0)
        time.sleep(1.0)  # Longer pause
    adJustLED(0)
    print("⚠️ Alternate warning blink complete.")

def warning_smooth_glow(duration=5, step=0.1):
    start_time = time.time()
    while time.time() - start_time < duration:
        # Fade in
        for brightness in range(0, 21, 1):
            adJustLED(brightness)
            time.sleep(step)
        # Fade out
        for brightness in range(20, -1, -1):
            adJustLED(brightness)
            time.sleep(step)
    adJustLED(0)
    print("⚠️ Smooth glow complete.")


