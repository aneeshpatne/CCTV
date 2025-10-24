import requests

def reset():
    res = requests.get("http://192.168.1.119/reset")
    