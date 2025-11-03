import requests
from requests.exceptions import RequestException

def change_quality(quality):
    """Change camera quality/resolution setting.
    
    Args:
        quality: Resolution value to set
        
    Raises:
        RequestException: If the request fails (timeout, connection error, etc.)
    """
    res = requests.get(f"http://192.168.1.119/control?var=framesize&val={quality}", timeout=2)
    res.raise_for_status()  # Raise exception for bad status codes
    return res
