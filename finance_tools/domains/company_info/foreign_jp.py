"""
JP Company Info Fetcher - 使用 yfinance 抓取日本公司的基本資料
"""
from typing import Dict, Any, List, Optional
from .foreign_base import BaseForeignCompanyFetcher
import finance_tools.config as config

class JPCompanyInfoFetcher(BaseForeignCompanyFetcher):
    """使用 yfinance 套件抓取日本公司基本資料。"""

    def __init__(self, company_jp_dir: str = None):
        company_jp_dir = company_jp_dir or str(config.JP_COMPANY_DIR)
        super().__init__(
            company_dir=company_jp_dir,
        )

    def _to_yahoo_ticker(self, code: str) -> str:
        """將目錄名（5201.JP）轉換為 Yahoo Finance ticker（5201.T）"""
        if code.endswith(".JP"):
            return code.replace(".JP", ".T")
        return code

    # JP Fetcher 可能有特定的欄位處理邏輯，如果有不同於 base 的，可以在此覆寫
    # 目前觀察 base implementation 應該足夠通用，或是只需微調
