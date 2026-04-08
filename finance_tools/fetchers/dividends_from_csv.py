"""
Dividends from CSV Fetcher - 從本地 CSV 檔案抓取股利數據
"""

import os
import logging
import pandas as pd
from typing import Dict, Any

logger = logging.getLogger(__name__)

class DividendsFromCSVFetcher:
    """從 CSV 檔案抓取並處理股利數據"""

    def __init__(self, finance_tools_dir: str = 'finance_tools'):
        self.finance_tools_dir = finance_tools_dir
        self.dividend_files = [
            {'path': os.path.join(self.finance_tools_dir, 'Dividend_csv_goodinfo', 'StockList整年_現金股利.csv'), 'type': 'cash'},
            {'path': os.path.join(self.finance_tools_dir, 'Dividend_csv_goodinfo', 'StockList整年_股票股利.csv'), 'type': 'stock'},
            {'path': os.path.join(self.finance_tools_dir, 'Dividend_csv_goodinfo', 'StockList半年_現金股利.csv'), 'type': 'cash'},
            {'path': os.path.join(self.finance_tools_dir, 'Dividend_csv_goodinfo', 'StockList半年_股票股利.csv'), 'type': 'stock'},
            {'path': os.path.join(self.finance_tools_dir, 'Dividend_csv_goodinfo', 'StockList逐季_現金股利.csv'), 'type': 'cash'},
            {'path': os.path.join(self.finance_tools_dir, 'Dividend_csv_goodinfo', 'StockList逐季_股票股利.csv'), 'type': 'stock'},
        ]

    def _parse_stock_code(self, code: str) -> str:
        """Helper to parse the stock code format, e.g., '="0050"' -> '0050'"""
        return code.replace('="', '').replace('"', '').strip()

    def fetch_all(self) -> Dict[str, Dict[str, Any]]:
        """
        從所有指定的 CSV 檔案中讀取、匯總並處理股利數據。

        Returns:
            一個字典，key 是公司代碼，value 是包含 'frequency' 和 'years' 的字典。
            e.g., { '2330': { 'frequency': '季', 'years': { 2023: { 'cash': 11.0, 'stock': 0 } } } }
        """
        all_dividends: Dict[str, Dict[str, Any]] = {}

        for file_info in self.dividend_files:
            if not os.path.exists(file_info['path']):
                logger.warning(f"File not found, skipping: {file_info['path']}")
                continue

            logger.info(f"Reading {file_info['path']}...")
            try:
                # Read CSV with utf-8-sig to handle potential BOM
                df = pd.read_csv(file_info['path'], encoding='utf-8-sig')
                
                # Find year columns (e.g., 2023發放年度)
                year_columns = {col: int(col.split('發放年度')[0]) for col in df.columns if '發放年度' in col and col.split('發放年度')[0].isdigit()}

                for _, row in df.iterrows():
                    code_raw = str(row.get('代號', ''))
                    if not code_raw or pd.isna(row.get('代號')):
                        continue
                    
                    code = self._parse_stock_code(code_raw)
                    
                    # Filter for valid 4-digit stock codes not starting with 0
                    if not (len(code) == 4 and code.isdigit() and not code.startswith('0')):
                         continue

                    if code not in all_dividends:
                        all_dividends[code] = {'years': {}}

                    # Set frequency, highest frequency will overwrite lower ones due to process order
                    frequency = row.get('股利發放頻率')
                    if pd.notna(frequency):
                        all_dividends[code]['frequency'] = str(frequency).strip()

                    for col_name, year in year_columns.items():
                        if year == 2026:  # Skip 2026 data as per user's request
                            continue
                        value = row.get(col_name)
                        # Ensure value is a number and not NaN
                        if pd.notna(value) and isinstance(value, (int, float)):
                            if year not in all_dividends[code]['years']:
                                all_dividends[code]['years'][year] = {'cash': 0.0, 'stock': 0.0}
                            
                            if file_info['type'] == 'cash':
                                all_dividends[code]['years'][year]['cash'] += float(value)
                            else:
                                all_dividends[code]['years'][year]['stock'] += float(value)

            except Exception as e:
                logger.error(f"Failed to process file {file_info['path']}: {e}")

        return all_dividends
