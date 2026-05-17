import json
import threading

from websockets.sync.client import connect

LED_WS_URL = "ws://192.168.0.13/led-ws"
LED_OPEN_TIMEOUT = 0.4
LED_RESPONSE_TIMEOUT = 0.4

_led_lock = threading.Lock()
_led_ws = None


def _close_led_ws() -> None:
    global _led_ws
    if _led_ws is None:
        return
    try:
        _led_ws.close()
    except Exception:
        pass
    finally:
        _led_ws = None


def _send_led_payload(payload: str) -> str:
    global _led_ws
    if _led_ws is None:
        _led_ws = connect(
            LED_WS_URL,
            open_timeout=LED_OPEN_TIMEOUT,
            close_timeout=0.2,
            ping_interval=None,
        )
    _led_ws.send(payload)
    return _led_ws.recv(timeout=LED_RESPONSE_TIMEOUT)


def adJustLED(Brightness: int):
    brightness = max(0, min(255, int(Brightness)))
    payload = json.dumps({"led_intensity": brightness})

    with _led_lock:
        try:
            response = _send_led_payload(payload)
        except Exception:
            _close_led_ws()
            try:
                response = _send_led_payload(payload)
            except Exception:
                _close_led_ws()
                return "Failure"

    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        return "Failure"

    if data.get("ok") is True and data.get("led_intensity") == brightness:
        return "Success"
    return "Failure"
