import requests

try:
    res = requests.get("https://example.com/api", timeout=5)
    data = res.json().get("framesize")
except requests.exceptions.Timeout:
    print("Request timed out after 5 seconds.")
    data = None
except (requests.exceptions.RequestException, ValueError, KeyError) as e:
    print(f"Error fetching data: {e}")
    data = None