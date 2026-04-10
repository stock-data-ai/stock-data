"""
MOPS Dividend Fetcher - 從公開資訊觀測站 (MOPS) 開放資料自動抓取最新股利公告
"""

import logging
import pandas as pd
import numpy as np
from typing import Dict, Any

logger = logging.getLogger(__name__)

class MOPSDividendFetcher:
    """從 MOPS 開放資料抓取並處理股利數據，支援季配息累加與公積加總。"""

    def __init__(self):
        self.urls = {
            "L": "https://mopsfin.twse.com.tw/opendata/t187ap45_L.csv", # 上市
            "O": "https://mopsfin.twse.com.tw/opendata/t187ap45_O.csv"  # 上櫃
        }

    def _safe_float(self, val):
        try:
            if pd.isna(val): return 0.0
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    def fetch_all(self) -> Dict[str, Dict[str, Any]]:
        """
        從 MOPS 抓取、加總並處理股利數據。
        
        Returns:
            { '2330': { 'years': { 2026: { 'cash': 6.0, 'stock': 0.0 } } } }
        """
        all_dividends: Dict[str, Dict[str, Any]] = {}
        dfs = []

        for category, url in self.urls.items():
            logger.info(f"Fetching {category} dividend data from MOPS...")
            try:
                # 使用 utf-8-sig 處理 BOM
                df = pd.read_csv(url, encoding='utf-8-sig')
                dfs.append(df)
            except Exception as e:
                logger.error(f"Failed to fetch {category} from {url}: {e}")

        if not dfs:
            return {}

        full_df = pd.concat(dfs, ignore_index=True)

        for _, row in full_df.iterrows():
            code = str(row.get("公司代號", "")).strip()
            if not code or len(code) < 4:
                continue

            # 1. 處理年度 (民國 114 -> 西元 2026)
            try:
                mops_year = int(row.get("股利年度", 0))
                if mops_year == 0: continue
                payout_year = mops_year + 1912
            except:
                continue

            # 2. 現金股利加總邏輯 (盈餘 + 法定公積 + 資本公積)
            cash_earnings = self._safe_float(row.get("股東配發-盈餘分配之現金股利(元/股)"))
            cash_legal = self._safe_float(row.get("股東配發-法定盈餘公積發放之現金(元/股)"))
            cash_capital = self._safe_float(row.get("股東配發-資本公積發放之現金(元/股)"))
            cash_total = cash_earnings + cash_legal + cash_capital

            # 3. 股票股利加總邏輯 (盈餘轉增資 + 法定公積轉增資 + 資本公積轉增資)
            stock_earnings = self._safe_float(row.get("股東配發-盈餘轉增資配股(元/股)"))
            stock_legal = self._safe_float(row.get("股東配發-法定盈餘公積轉增資配股(元/股)"))
            stock_capital = self._safe_float(row.get("股東配發-資本公積轉增資配股(元/股)"))
            stock_total = stock_earnings + stock_legal + stock_capital

            # 4. 初始化結構
            if code not in all_dividends:
                all_dividends[code] = {'years': {}}
            
            if payout_year not in all_dividends[code]['years']:
                all_dividends[code]['years'][payout_year] = {'cash': 0.0, 'stock': 0.0}

            # 5. 累加邏輯 (處理季配息/半年配)
            # 同一個年度的多筆紀錄會被自動加總
            all_dividends[code]['years'][payout_year]['cash'] += cash_total
            all_dividends[code]['years'][payout_year]['stock'] += stock_total

            # 6. 紀錄頻率 (選填，若有標示期別則更新)
            period = str(row.get("股利所屬年(季)度", ""))
            if "季" in period:
                all_dividends[code]['frequency'] = "季"
            elif "半年" in period:
                all_dividends[code]['frequency'] = "半年"
            elif "年" in period and 'frequency' not in all_dividends[code]:
                all_dividends[code]['frequency'] = "年"

        logger.info(f"Processed {len(all_dividends)} companies from MOPS.")
        return all_dividends
