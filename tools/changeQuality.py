import requests

def change_quality(quality):
    res = requests.get(f"http://192.168.1.119/control?var=framesize&val={quality}", timeout=2)
