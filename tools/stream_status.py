from tools.jpeg_ws_capture import JPEG_WS_URL, JpegWebSocketCapture

url = JPEG_WS_URL


def check_mjpeg_stream(url=url, timeout=2):
    try:
        cap = JpegWebSocketCapture(url, open_timeout=timeout, read_timeout=timeout)
        if not cap.open():
            return False, "websocket open failed"
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            return False, "invalid jpeg websocket frame"
        return True, "websocket jpeg stream"
    except Exception as e:
        return False, str(e)
