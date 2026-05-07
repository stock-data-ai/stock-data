"""
Base Foreign Company Fetcher - 使用 yfinance 抓取外國公司資料的基底類別
"""
import os
import json
import logging
import yfinance as yf
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

class BaseForeignCompanyFetcher:
    """
    使用 yfinance 套件抓取外國公司基本資料的基底類別。
    封裝了通用的抓取、錯誤處理與資料解析邏輯。
    """

    def __init__(self, company_dir: str, company_list_file: str = None):
        self.company_dir = company_dir
        self.company_list_file = company_list_file

    def get_company_codes(self) -> List[str]:
        """從同步清單或指定目錄取得所有公司代碼"""
        if self.company_list_file and os.path.exists(self.company_list_file):
            try:
                with open(self.company_list_file, "r", encoding="utf-8") as f:
                    company_list = json.load(f)

                codes = []
                for item in company_list:
                    if isinstance(item, str):
                        code = item
                    elif isinstance(item, dict):
                        code = item.get("code")
                    else:
                        code = None

                    if code:
                        codes.append(str(code).upper())

                return sorted(set(codes))
            except Exception as e:
                logger.error(f"Failed to read company list {self.company_list_file}: {e}")
                return []

        if not os.path.exists(self.company_dir):
            logger.error(f"Directory not found: {self.company_dir}")
            return []
        codes = [
            d for d in sorted(os.listdir(self.company_dir))
            if os.path.isdir(os.path.join(self.company_dir, d))
            and not d.startswith(".")
        ]
        return codes

    def _to_yahoo_ticker(self, code: str) -> str:
        """
        將公司代碼轉換為 Yahoo Finance ticker。
        預設回傳原代碼，子類別可視需求覆寫 (例如 JP 需要加 .T)
        """
        return code

    def _fetch_one(self, code: str) -> Optional[Dict[str, Any]]:
        """使用 yfinance 抓取單一公司的基本資料"""
        yahoo_ticker = self._to_yahoo_ticker(code)
        try:
            t = yf.Ticker(yahoo_ticker)
            info = t.info
            
            # yfinance 有時回傳空的或無效的 info，這裡做簡單的有效性檢查
            # 例如檢查 shortName 或 trailingPegRatio 是否存在
            if not info or (info.get("trailingPegRatio") is None and info.get("shortName") is None):
                if not info.get("shortName"):
                    logger.warning(f"{code} ({yahoo_ticker}): No valid data (shortName missing)")
                    return None
            return info
        except Exception as e:
            logger.error(f"{code} ({yahoo_ticker}): {e}")
            return None

    def _build_company_dict(self, code: str, info: Dict[str, Any]) -> Dict[str, Any]:
        """
        將 yfinance info 轉換為專案標準的公司資料字典格式。
        """
        # 公司管理層提取
        officers = info.get("companyOfficers", [])
        ceo = next(
            (o.get("name") for o in officers
             if "CEO" in (o.get("title") or "").upper()
             or "Chief Executive" in (o.get("title") or "")
             or "President" in (o.get("title") or "")),
            officers[0].get("name") if officers else None,
        )
        cfo = next(
            (o.get("name") for o in officers
             if "CFO" in (o.get("title") or "").upper()
             or "Chief Financial" in (o.get("title") or "")),
            None,
        )

        # 地址組合
        address_parts = [
            info.get("address1"),
            info.get("address2"),
            info.get("city"),
            info.get("state"),
            info.get("zip"),
            info.get("country"),
        ]
        full_address = ", ".join(str(p) for p in address_parts if p)

        return {
            "name": info.get("longName") or info.get("shortName") or code,
            "code": code,
            "shortName": info.get("shortName") or code,
            "website": info.get("website"),
            "foundedDate": None,  # Yahoo Finance 通常不提供精確的成立日期
            "gov": {
                "profile": {
                    "companyCode": code,
                    "companyName": info.get("longName") or code,
                    "companyShortName": info.get("shortName") or code,
                    "industry": {
                        "code": info.get("industryKey"),
                        "name": info.get("industry"),
                    },
                    "sector": {
                        "code": info.get("sectorKey"),
                        "name": info.get("sector"),
                    },
                    "taxId": None, # 外國公司通常無統一編號
                },
                "management": {
                    "ceo": ceo,
                    "cfo": cfo,
                },
                "contact": {
                    "address": full_address or None,
                    "phone": info.get("phone"),
                    "website": info.get("website"),
                    "country": info.get("country"),
                    "city": info.get("city"),
                    "state": info.get("state"),
                },
                "listing": {
                    "exchange": info.get("exchange"),
                    "exchangeName": info.get("exchangeTimezoneName"),
                    "quoteType": info.get("quoteType"),
                    "currency": info.get("currency"),
                },
                "capital": {
                    "marketCap": info.get("marketCap"),
                    "fullTimeEmployees": info.get("fullTimeEmployees"),
                    "sharesOutstanding": info.get("sharesOutstanding"),
                },
                "description": info.get("longBusinessSummary"),
            },
        }

    def fetch_all(self, codes: Optional[List[str]] = None) -> Dict[str, Any]:
        """抓取所有（或指定的）公司的基本資料"""
        if codes is None:
            codes = self.get_company_codes()

        if not codes:
            logger.warning("No company codes to fetch.")
            return {}

        logger.info(f"Fetching company info for {len(codes)} companies...")
        result = {}
        for i, code in enumerate(codes, 1):
            logger.info(f"  [{i}/{len(codes)}] {code}...")
            info = self._fetch_one(code)
            if info:
                result[code] = self._build_company_dict(code, info)
                logger.info(f"  [{i}/{len(codes)}] {code}... OK")
            else:
                logger.info(f"  [{i}/{len(codes)}] {code}... SKIP")

        logger.info(f"Fetched {len(result)}/{len(codes)} companies.")
        return result
