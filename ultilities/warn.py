from tools.adjustLED import adJustLED
import time
def warning_blink(duration=5, interval=0.5):
    start_time = time.time()
    while time.time() - start_time < duration:
        adJustLED(20)  
        time.sleep(interval)
        adJustLED(0)     
        time.sleep(interval)
    adJustLED(0)
    print("⚠️ Warning blink complete.")


def warning_blink_alternate(duration=5, interval=0.5):
    start_time = time.time()
    while time.time() - start_time < duration:
        # Pattern: quick double flash
        adJustLED(20)
        time.sleep(0.2)
        adJustLED(0)
        time.sleep(0.2)
        adJustLED(20)
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


