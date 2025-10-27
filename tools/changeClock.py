import requests

XCLK_ENDPOINT = "http://192.168.1.119/xclk"


def change_clock(xclk: int = 15) -> None:
    """Set the camera external clock to the requested value."""
    params = {"xclk": xclk}
    requests.get(XCLK_ENDPOINT, params=params, timeout=2)
