import json

from websockets.sync.client import connect

LED_WS_URL = "ws://192.168.0.13/led-ws"


def adJustLED(Brightness: int):
    brightness = max(0, min(255, int(Brightness)))
    payload = json.dumps({"led_intensity": brightness})

    try:
        with connect(LED_WS_URL, open_timeout=1, close_timeout=1) as websocket:
            websocket.send(payload)
            response = websocket.recv(timeout=1)
    except Exception:
        return "Failure"

    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        return "Failure"

    if data.get("ok") is True and data.get("led_intensity") == brightness:
        return "Success"
    return "Failure"
