import cv2
import numpy as np
from websockets.sync.client import connect

JPEG_WS_URL = "ws://192.168.0.13:81/jpeg-ws"


class JpegWebSocketCapture:
    """VideoCapture-like wrapper for the ESP32 binary JPEG WebSocket stream."""

    def __init__(
        self,
        url: str = JPEG_WS_URL,
        open_timeout: float = 10.0,
        read_timeout: float = 6.0,
    ):
        self.url = url
        self.open_timeout = open_timeout
        self.read_timeout = read_timeout
        self.websocket = None
        self.opened = False
        self.last_frame_shape = None

    def open(self) -> bool:
        self.release()
        try:
            self.websocket = connect(
                self.url,
                open_timeout=self.open_timeout,
                close_timeout=1,
                ping_interval=None,
                max_size=4 * 1024 * 1024,
            )
            self.websocket.send("start")
            self.opened = True
            return True
        except Exception as exc:
            print(f"JPEG WebSocket open failed: {exc}")
            self.release()
            return False

    def isOpened(self) -> bool:
        return self.opened and self.websocket is not None

    def read(self):
        if not self.isOpened():
            return False, None
        try:
            message = self.websocket.recv(timeout=self.read_timeout)
        except Exception as exc:
            print(f"JPEG WebSocket read failed: {exc}")
            self.release()
            return False, None

        if not isinstance(message, bytes):
            print("JPEG WebSocket received non-binary message")
            return False, None

        encoded = np.frombuffer(message, dtype=np.uint8)
        frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if frame is None:
            print("JPEG WebSocket frame decode failed")
            return False, None
        self.last_frame_shape = frame.shape
        return True, frame

    def set(self, _prop_id, _value) -> bool:
        return True

    def get(self, prop_id) -> float:
        if self.last_frame_shape is None:
            return 0.0
        height, width = self.last_frame_shape[:2]
        if prop_id == cv2.CAP_PROP_FRAME_WIDTH:
            return float(width)
        if prop_id == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(height)
        return 0.0

    def release(self) -> None:
        self.opened = False
        if self.websocket is None:
            return
        try:
            self.websocket.close()
        except Exception:
            pass
        finally:
            self.websocket = None
