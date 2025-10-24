import requests

def status():
    try:
        res = requests.get("http://192.168.1.119/status", timeout=5)
        data = res.json().get("framesize")
        return data
    except requests.exceptions.Timeout:
        print("Request timed out after 5 seconds.")
        data = None
    except (requests.exceptions.RequestException, ValueError, KeyError) as e:
        print(f"Error fetching data: {e}")
        data = None