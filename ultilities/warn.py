from tools.adjustLED import adJustLED
import time
def warning_blink(duration=5, interval=0.5):
    start_time = time.time()
    while time.time() - start_time < duration:
        adJustLED(20)   # Full brightness (ON)
        time.sleep(interval)
        adJustLED(0)     # Off
        time.sleep(interval)
    adJustLED(0)
    print("⚠️ Warning blink complete.")


