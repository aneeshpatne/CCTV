import requests

def status():
    try:
        res = requests.get("http://192.168.1.13/status", timeout=2)
        data = res.json().get("framesize")
    except requests.exceptions.Timeout:
        data = None
    except (requests.exceptions.RequestException, ValueError, KeyError) as e:
        data = None
    return data
    