import time
import yfinance as yf
import pandas as pd
import logging
from typing import Tuple, Optional, Dict, Any
from tenacity import retry, stop_after_attempt, wait_exponential

# 靜音 yfinance 內部的 HTTP Error 404 日誌，避免 OTC 股票切換時產生噪音
logging.getLogger('yfinance').setLevel(logging.CRITICAL)

logger = logging.getLogger(__name__)

class YahooFetcher:
    """
    Fetcher for stock data using Yahoo Finance (yfinance).
    Handles both Taiwan stocks (.TW/.TWO) and potentially global stocks.
    """

    @staticmethod
    def to_yahoo_symbol(stock_id: str) -> str:
        """
        Converts a standard stock ID to a Yahoo Finance symbol.
        Defaults to .TW (Listed) but logic can be extended.
        """
        if stock_id.endswith(".TW") or stock_id.endswith(".TWO"):
            return stock_id
        
        # Note: In a real scenario, we might want to check if it's listed or over-the-counter.
        # For now, we provide a helper but yfinance might need the correct suffix.
        # Common pattern in this project is 4-digit codes for Taiwan stocks.
        if len(stock_id) == 4 and stock_id.isdigit():
            return f"{stock_id}.TW"
        return stock_id

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True
    )
    def fetch_price_history(
        self, stock_id: str, period: str = "1mo", interval: str = "1d"
    ) -> pd.DataFrame:
        """
        Fetches historical price data.
        period: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max
        """
        symbol = self.to_yahoo_symbol(stock_id)
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=period, interval=interval)
            
            # If no data and it's a 4-digit code, try .TWO (OTC)
            if df.empty and symbol.endswith(".TW"):
                alt_symbol = symbol.replace(".TW", ".TWO")
                logger.debug(f"No data for {symbol}, trying {alt_symbol}")
                ticker = yf.Ticker(alt_symbol)
                df = ticker.history(period=period, interval=interval)

            if df.empty:
                logger.warning(f"No price history found for {stock_id} via Yahoo Finance.")
                return pd.DataFrame()
            
            return df
        except Exception as e:
            logger.error(f"Error fetching Yahoo price history for {stock_id}: {e}")
            return pd.DataFrame()

    def get_latest_price(self, stock_id: str) -> Optional[float]:
        """
        Gets the most recent closing price.
        """
        df = self.fetch_price_history(stock_id, period="5d") # Fetch a few days to ensure we get the last close
        if not df.empty:
            return float(df['Close'].iloc[-1])
        return None

    def _get_ticker_info(self, symbol: str) -> Optional[dict]:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        return info if info and "marketCap" in info else None

    def fetch_market_stats(self, stock_id: str) -> Dict[str, Any]:
        """Fetches market cap, PE, PB from Yahoo. Retries 3x, returns {} on all failures."""
        symbol = self.to_yahoo_symbol(stock_id)
        alt_symbol = symbol.replace(".TW", ".TWO") if symbol.endswith(".TW") else None

        info = None
        for attempt in range(3):
            try:
                info = self._get_ticker_info(symbol)
                if info is None and alt_symbol:
                    info = self._get_ticker_info(alt_symbol)
                if info:
                    break
            except Exception as e:
                logger.debug(f"Yahoo {stock_id} attempt {attempt + 1}/3: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)

        if not info:
            return {}

        return {
            "marketCap": info.get("marketCap"),
            "trailingPE": info.get("trailingPE"),
            "priceToBook": info.get("priceToBook"),
            "dividendYield": info.get("dividendYield"),
            "beta": info.get("beta"),
            "currentPrice": info.get("currentPrice") or info.get("regularMarketPrice"),
        }
