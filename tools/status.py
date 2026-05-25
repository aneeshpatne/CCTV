from utilities.esp32cam_client import get_camera_status


def status():
    camera_status = get_camera_status(timeout=2)
    if not camera_status.camera_online:
        return None
    return camera_status.framesize
