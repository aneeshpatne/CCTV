from tools.mjpeg_capture import MJPEG_STREAM_URL, MjpegStreamCapture

url = MJPEG_STREAM_URL


def check_mjpeg_stream(url=url, timeout=2):
    try:
        cap = MjpegStreamCapture(url, open_timeout=timeout, read_timeout=timeout)
        if not cap.open():
            return False, "mjpeg stream open failed"
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            return False, "invalid mjpeg frame"
        return True, "http mjpeg stream"
    except Exception as e:
        return False, str(e)
