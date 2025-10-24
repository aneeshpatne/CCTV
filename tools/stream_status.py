import requests

url = "http://192.168.1.119:81/stream"
try:
    res = requests.get(url, stream=True, timeout=2)
    ctype = res.headers.get("Content-Type", "")
    if "multipart/x-mixed-replace" in ctype.lower() and "boundary=" in ctype.lower():
        print("✅ MJPEG stream appears valid:", ctype)
    else:
        print("⚠️ Unexpected content type:", ctype)
except requests.exceptions.RequestException as e:
    print(f"❌ Request failed: {e}")