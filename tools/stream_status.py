import requests

url = "http://192.168.0.13:81/stream"


def check_mjpeg_stream(url=url, timeout=2):
    try:
        res = requests.get(url, stream=True, timeout=timeout)
        ctype = res.headers.get("Content-Type", "")
        if (
            "multipart/x-mixed-replace" in ctype.lower()
            and "boundary=" in ctype.lower()
        ):
            return True, ctype
        else:
            return False, ctype
    except requests.exceptions.RequestException as e:
        return False, str(e)
