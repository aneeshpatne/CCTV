import requests

def adJustLED(Brightness: int):
    response = requests.get(f"http://192.168.1.116/control?var=led_intensity&val={Brightness}")
    if response.status_code == 200:
        return "Success"
    return "Failure"


