"""
MOPS Dividend Fetcher - 從公開資訊觀測站 (MOPS) 開放資料自動抓取最新股利公告
"""

import logging
import pandas as pd
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


def _roc_to_date(val) -> Optional[str]:
    """民國 8 碼整數 (e.g. 1150210) → 'YYYY-MM-DD'，失敗回 None"""
    try:
        s = str(int(val)).zfill(7)
        y = int(s[:3]) + 1911
        m = s[3:5]
        d = s[5:7]
        return f"{y}-{m}-{d}"
    except Exception:
        return None


class MOPSDividendFetcher:
    """從 MOPS 開放資料抓取並處理股利數據，每筆配息獨立儲存（支援季/半年/年配）。"""

    def __init__(self):
        self.urls = {
            "L": "https://mopsfin.twse.com.tw/opendata/t187ap45_L.csv",  # 上市
            "O": "https://mopsfin.twse.com.tw/opendata/t187ap45_O.csv",  # 上櫃
        }

    def _safe_float(self, val):
        try:
            if pd.isna(val):
                return 0.0
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    def fetch_all(self) -> Dict[str, Dict[str, Any]]:
        """
        從 MOPS 抓取股利數據，每筆配息獨立儲存。

        Returns:
            {
              '2330': {
                'records': [
                  {
                    'year': 2026,
                    'period': '第4季',
                    'periodRange': '2025-10-01~2025-12-31',
                    'sequence': 1,
                    'cashDividend': 6.0,
                    'stockDividend': 0.0,
                    'totalDividend': 6.0,
                    'payoutDate': '2026-02-10',
                    'agmDate': None,
                    'status': '董事會決議',
                  }
                ]
              }
            }
        """
        all_data: Dict[str, Dict[str, Any]] = {}
        dfs = []

        for category, url in self.urls.items():
            logger.info(f"Fetching {category} dividend data from MOPS...")
            try:
                df = pd.read_csv(url, encoding="utf-8-sig")
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

            # 年度 (民國 → 西元配發年，+1912)
            try:
                mops_year = int(row.get("股利年度", 0))
                if mops_year == 0:
                    continue
                payout_year = mops_year + 1912
            except Exception:
                continue

            # 現金股利加總（盈餘 + 法定公積 + 資本公積）
            cash = (
                self._safe_float(row.get("股東配發-盈餘分配之現金股利(元/股)"))
                + self._safe_float(row.get("股東配發-法定盈餘公積發放之現金(元/股)"))
                + self._safe_float(row.get("股東配發-資本公積發放之現金(元/股)"))
            )
            # 股票股利加總
            stock = (
                self._safe_float(row.get("股東配發-盈餘轉增資配股(元/股)"))
                + self._safe_float(row.get("股東配發-法定盈餘公積轉增資配股(元/股)"))
                + self._safe_float(row.get("股東配發-資本公積轉增資配股(元/股)"))
            )

            # 股利所屬期間（民國轉西元）
            period_range_raw = str(row.get("股利所屬期間", "") or "").strip()
            period_range = None
            if "~" in period_range_raw and len(period_range_raw) >= 15:
                try:
                    start, end = period_range_raw.split("~")
                    def roc_ymd(s):
                        s = s.strip().zfill(7)
                        return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:7]}"
                    period_range = f"{roc_ymd(start)}~{roc_ymd(end)}"
                except Exception:
                    pass

            record = {
                "year": payout_year,
                "period": str(row.get("股利所屬年(季)度", "") or "").strip() or None,
                "periodRange": period_range,
                "sequence": int(row.get("期別", 1) or 1),
                "cashDividend": round(cash, 4),
                "stockDividend": round(stock, 4),
                "totalDividend": round(cash + stock, 4),
                "payoutDate": _roc_to_date(row.get("董事會（擬議）股利分派日")),
                "agmDate": _roc_to_date(row.get("股東會日期")),
                "status": str(row.get("決議（擬議）進度", "") or "").strip() or None,
            }

            if code not in all_data:
                all_data[code] = {"records": []}
            all_data[code]["records"].append(record)

        # 每家公司的 records 按 (year desc, sequence asc) 排序
        for code in all_data:
            all_data[code]["records"].sort(key=lambda r: (r["year"], r["sequence"]))

        logger.info(f"Processed {len(all_data)} companies from MOPS.")
        return all_data
