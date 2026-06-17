import requests


def adJustLED(Brightness: int):
    try:
        response = requests.get(
            f"http://192.168.0.13/control?var=led_intensity&val={Brightness}",
            timeout=1.0,
        )
        if response.status_code == 200:
            return "Success"
        return "Failure"
    except (requests.RequestException, OSError):
        return "Failure"
