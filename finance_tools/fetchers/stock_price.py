import time
import random
import logging
from typing import Optional
import requests
import config

logger = logging.getLogger(__name__)

def get_latest_stock_price(code: str) -> Optional[float]:
    """
    Fetches the latest closing stock price for a given company code from TWSE or TPEx.
    """
    # Try TWSE (上市)
    try:
        url_twse = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL"
        # Increased timeout to 10 seconds for potentially slower responses
        resp_twse = requests.get(url_twse, timeout=10) 
        resp_twse.raise_for_status()

        payload_twse = resp_twse.json()
        if payload_twse.get("data"):
            fields = payload_twse.get("fields", [])
            for row in payload_twse["data"]:
                if len(row) < len(fields):
                    row += [None] * (len(fields) - len(row))
                record = dict(zip(fields, row))
                if record.get("證券代號") == code:
                    closing_price_str = record.get("收盤價")
                    if closing_price_str is not None:
                        try:
                            # Remove comma from string, then convert to float
                            return float(str(closing_price_str).replace(',', ''))
                        except ValueError:
                            pass # Fallback to other sources
    except requests.exceptions.RequestException as e:
        logger.warning(f"TWSE fetch failed for {code}: {e}")
    
    time.sleep(random.uniform(*config.DEFAULT_SLEEP_RANGE)) # Be nice to APIs

    # Try TPEx (上櫃)
    try:
        url_tpex = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
        resp_tpex = requests.get(url_tpex, timeout=10) # Increased timeout
        resp_tpex.raise_for_status()
        rows_tpex = resp_tpex.json()
        for record in rows_tpex:
            if record.get("SecuritiesCompanyCode") == code:
                closing_price = record.get("ClosePrice")
                if closing_price is not None:
                    try:
                        return float(closing_price)
                    except ValueError:
                        pass # Fallback
    except requests.exceptions.RequestException as e:
        logger.warning(f"TPEx fetch failed for {code}: {e}")

    # No price found
    return None
