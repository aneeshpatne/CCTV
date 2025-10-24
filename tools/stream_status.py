import requests

url = "http://192.168.1.119:81/stream"
def check_mjpeg_stream(url=url, timeout=2):
    try:
        res = requests.get(url, stream=True, timeout=timeout)
        ctype = res.headers.get("Content-Type", "")
        if "multipart/x-mixed-replace" in ctype.lower() and "boundary=" in ctype.lower():
            print("✅ MJPEG stream appears valid:", ctype)
            return True, ctype
        else:
            print("⚠️ Unexpected content type:", ctype)
            return False, ctype
    except requests.exceptions.RequestException as e:
        print(f"❌ Request failed: {e}")
        return False, str(e)