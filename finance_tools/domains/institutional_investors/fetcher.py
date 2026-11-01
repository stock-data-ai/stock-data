import logging
import requests
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

BASE_URL = "https://voidful.github.io/tw-institutional-stocker/data/"

def fetch_top_three_institutional_changes(window: int, up: bool = True) -> List[Dict[str, Any]]:
    """
    Fetches the top institutional investor changes for a given window.

    Args:
        window (int): The window of days (e.g., 5, 20, 60, 120).
        up (bool): If True, fetches 'up' changes; if False, fetches 'down' changes.

    Returns:
        List[Dict[str, Any]]: A list of dictionaries containing the institutional change data.
    """
    direction = "up" if up else "down"
    url = f"{BASE_URL}top_three_inst_change_{window}_{direction}.json"
    try:
        response = requests.get(url)
        response.raise_for_status()  # Raise an HTTPError for bad responses (4xx or 5xx)
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching data from {url}: {e}")
        return []

if __name__ == "__main__":
    # Example usage:
    print("Fetching top 5-day UP institutional changes:")
    five_day_up = fetch_top_three_institutional_changes(5, True)
    if five_day_up:
        print(f"First item: {five_day_up[0]}")
    else:
        print("No data fetched.")
