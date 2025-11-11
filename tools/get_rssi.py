"""RSSI (signal strength) monitoring tool for ESP32-CAM.

Fetches WiFi signal strength from the camera's /rssi endpoint.
Returns signal strength in dBm (-30 to -90, where higher values = better signal).
"""

import requests


def get_rssi(timeout: float = 2.0) -> int | None:
    """Fetch RSSI value from ESP32-CAM.
    
    Args:
        timeout: Request timeout in seconds
        
    Returns:
        RSSI value in dBm (e.g., -50) or None if request fails
    """
    try:
        res = requests.get("http://192.168.1.13/rssi", timeout=timeout)
        data = res.json().get("rssi")
        return data if isinstance(data, int) else None
    except requests.exceptions.Timeout:
        return None
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return None


if __name__ == "__main__":
    rssi = get_rssi()
    if rssi is not None:
        print(f"RSSI: {rssi} dBm")
    else:
        print("Failed to fetch RSSI")
