import requests
import pandas as pd
import logging
import time
from typing import Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)

class MarginTradingFetcher:
    """
    Fetcher for Margin Trading data from TWSE (Listed) and TPEx (OTC).
    """
    
    TWSE_URL = "https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json&date={date}&selectType=ALL"
    TPEX_URL = "https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php?l=zh-tw&d={roc_date}&_={timestamp}"

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.twse.com.tw/",
        }

    def _clean_number(self, val: Any) -> Any:
        if isinstance(val, str):
            return val.replace(',', '')
        return val

    def fetch_twse(self, date_str: str) -> Optional[pd.DataFrame]:
        """
        Fetch Listed (TWSE) margin trading data.
        date_str: YYYYMMDD
        """
        url = self.TWSE_URL.format(date=date_str)
        try:
            response = requests.get(url, headers=self.headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            if data.get('stat') != 'OK':
                logger.warning(f"TWSE Margin Trading API returned status: {data.get('stat')} for date {date_str}")
                return None
            
            target_table = None
            for table in data.get('tables', []):
                if '融資融券彙總' in table.get('title', ''):
                    target_table = table
                    break
            
            if not target_table:
                return None
            
            df = pd.DataFrame(target_table['data'], columns=target_table['fields'])
            
            # Column mapping for TWSE
            # 0: Code, 1: Name, 2: MarginBuy, 3: MarginSell, 4: MarginCashRepay, 6: MarginBalance
            # 8: ShortBuy, 9: ShortSell, 10: ShortStockRepay, 12: ShortBalance
            result = df.iloc[:, [0, 1, 2, 3, 4, 6, 8, 9, 10, 12]].copy()
            result.columns = [
                'stock_id', 'stock_name', 
                'margin_buy', 'margin_sell', 'margin_repay', 'margin_balance',
                'short_buy', 'short_sell', 'short_repay', 'short_balance'
            ]
            
            for col in result.columns[2:]:
                result[col] = pd.to_numeric(result[col].apply(self._clean_number), errors='coerce')
                
            return result
        except Exception as e:
            logger.error(f"Error fetching TWSE margin trading for {date_str}: {e}")
            return None

    def fetch_tpex(self, date_str: str) -> Optional[pd.DataFrame]:
        """
        Fetch OTC (TPEx) margin trading data.
        date_str: YYYYMMDD
        TPEx API fields: 代號,名稱,前資餘額,資買(3),資賣(4),現償(5),資餘額(6),...,前券餘額,券賣(11),券買(12),券償(13),券餘額(14),...
        """
        try:
            dt = datetime.strptime(date_str, '%Y%m%d')
            roc_date = f"{dt.year - 1911}/{dt.month:02}/{dt.day:02}"
        except Exception:
            return None

        timestamp = int(time.time() * 1000)
        url = self.TPEX_URL.format(roc_date=roc_date, timestamp=timestamp)

        try:
            response = requests.get(url, headers=self.headers, timeout=15)
            response.raise_for_status()
            data = response.json()

            # API 現為 tables 格式（同 TWSE），舊版 aaData 已棄用
            tables = data.get('tables', [])
            if not tables or not tables[0].get('data'):
                logger.warning(f"TPEx Margin Trading API returned no data for date {date_str}")
                return None

            table = tables[0]
            df = pd.DataFrame(table['data'], columns=table['fields'])

            # 0:代號 1:名稱 3:資買 4:資賣 5:現償 6:資餘額 11:券賣 12:券買 13:券償 14:券餘額
            result = df.iloc[:, [0, 1, 3, 4, 5, 6, 12, 11, 13, 14]].copy()
            result.columns = [
                'stock_id', 'stock_name',
                'margin_buy', 'margin_sell', 'margin_repay', 'margin_balance',
                'short_buy', 'short_sell', 'short_repay', 'short_balance'
            ]

            for col in result.columns[2:]:
                result[col] = pd.to_numeric(result[col].apply(self._clean_number), errors='coerce')

            return result
        except Exception as e:
            logger.error(f"Error fetching TPEx margin trading for {date_str}: {e}")
            return None

    def fetch_all(self, date_str: str) -> Optional[pd.DataFrame]:
        """
        Fetch both TWSE and TPEx margin trading data and combine them.
        """
        twse_df = self.fetch_twse(date_str)
        tpex_df = self.fetch_tpex(date_str)
        
        dfs = []
        if twse_df is not None:
            dfs.append(twse_df)
        if tpex_df is not None:
            dfs.append(tpex_df)
            
        if not dfs:
            return None
            
        return pd.concat(dfs, ignore_index=True)

if __name__ == "__main__":
    # Test fetcher
    logging.basicConfig(level=logging.INFO)
    fetcher = MarginTradingFetcher()
    test_date = "20240510"
    
    print(f"Testing fetch_all for {test_date}...")
    combined_df = fetcher.fetch_all(test_date)
    if combined_df is not None:
        print(f"Total rows fetched: {len(combined_df)}")
        print(combined_df.head())
        # Check for an OTC stock (e.g., 6488 GlobalWafers)
        otc_stock = combined_df[combined_df['stock_id'] == '6488']
        if not otc_stock.empty:
            print("\nOTC Stock (6488) Found:")
            print(otc_stock.to_dict('records')[0])
    else:
        print("Fetch failed")
