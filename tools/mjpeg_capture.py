import re
from typing import Optional

import cv2
import numpy as np
import requests

from utilities.esp32cam_client import CAMERA_STREAM_URL

MJPEG_STREAM_URL = CAMERA_STREAM_URL


class MjpegStreamCapture:
    """VideoCapture-like wrapper for the ESP32 HTTP MJPEG stream."""

    def __init__(
        self,
        url: str = MJPEG_STREAM_URL,
        open_timeout: float = 10.0,
        read_timeout: float = 6.0,
    ):
        self.url = url
        self.open_timeout = open_timeout
        self.read_timeout = read_timeout
        self.session = requests.Session()
        self.response: Optional[requests.Response] = None
        self.stream = None
        self.opened = False
        self.boundary = b"frame"
        self.last_frame_shape = None

    def open(self) -> bool:
        self.release()
        try:
            self.response = self.session.get(
                self.url,
                stream=True,
                timeout=(self.open_timeout, self.read_timeout),
            )
            self.response.raise_for_status()
            content_type = self.response.headers.get("Content-Type", "")
            match = re.search(r"boundary=(?P<boundary>[^;]+)", content_type, re.I)
            if match:
                self.boundary = match.group("boundary").strip('"').encode("ascii")
            self.stream = self.response.raw
            self.opened = True
            return True
        except Exception as exc:
            print(f"MJPEG stream open failed: {exc}")
            self.release()
            return False

    def isOpened(self) -> bool:
        return self.opened and self.response is not None and self.stream is not None

    def read(self):
        if not self.isOpened():
            return False, None

        try:
            jpeg = self._read_next_jpeg()
        except Exception as exc:
            print(f"MJPEG stream read failed: {exc}")
            self.release()
            return False, None

        if not jpeg:
            return False, None

        encoded = np.frombuffer(jpeg, dtype=np.uint8)
        frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if frame is None:
            print("MJPEG frame decode failed")
            return False, None
        self.last_frame_shape = frame.shape
        return True, frame

    def _read_next_jpeg(self) -> bytes:
        while True:
            line = self.stream.readline()
            if not line:
                return b""
            if line.strip() in (b"--" + self.boundary, self.boundary):
                break

        content_length = None
        while True:
            line = self.stream.readline()
            if not line:
                return b""
            line = line.strip()
            if not line:
                break
            name, _, value = line.partition(b":")
            if name.lower() == b"content-length":
                content_length = int(value.strip())

        if content_length is not None:
            jpeg = self.stream.read(content_length)
            self.stream.readline()
            return jpeg

        chunks = []
        while True:
            line = self.stream.readline()
            if not line:
                return b"".join(chunks)
            if line.startswith(b"--" + self.boundary):
                return b"".join(chunks)
            chunks.append(line)

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
        if self.response is not None:
            try:
                self.response.close()
            except Exception:
                pass
        self.response = None
        self.stream = None
