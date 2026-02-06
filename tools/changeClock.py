import requests
from requests.exceptions import RequestException

XCLK_ENDPOINT = "http://192.168.0.13/xclk"


def change_clock(xclk: int = 15) -> None:
    """Set the camera external clock to the requested value.

    Args:
        xclk: Clock value to set (default: 15)

    Raises:
        RequestException: If the request fails (timeout, connection error, etc.)
    """
    params = {"xclk": xclk}
    res = requests.get(XCLK_ENDPOINT, params=params, timeout=2)
    res.raise_for_status()
