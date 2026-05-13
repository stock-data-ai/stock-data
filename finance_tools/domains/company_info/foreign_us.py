"""
US Company Info Fetcher - 使用 yfinance 抓取美國公司的基本資料
"""
from .foreign_base import BaseForeignCompanyFetcher
import finance_tools.config as config

class USCompanyInfoFetcher(BaseForeignCompanyFetcher):
    """使用 yfinance 套件抓取美國公司基本資料。"""

    def __init__(self, company_us_dir: str = None):
        super().__init__(
            company_dir=company_us_dir or str(config.US_COMPANY_DIR),
        )
    
    # US Fetcher default behavior in base class should be sufficient
    # Ticker conversion is 1:1 (identity) by default
    
    def _to_yahoo_ticker(self, code: str) -> str:
        """US codes typically match Yahoo tickers directly (e.g. AAPL)"""
        return code

    def _build_company_dict(self, code: str, info: dict) -> dict:
        data = super()._build_company_dict(code, info)
        data["shortName"] = code
        gov = data.get("gov") or {}
        profile = gov.get("profile") or {}
        profile["companyShortName"] = code
        gov["profile"] = profile
        data["gov"] = gov
        return data
