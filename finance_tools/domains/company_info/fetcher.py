"""
Company Info Fetcher - 從台灣證券交易所開放資料抓取所有上市櫃公司的基本資料
"""
import logging
import pandas as pd
import numpy as np
import re
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

# ========= 產業代碼 → 產業名稱（證交所） =========
INDUSTRY_CODE_MAP = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "13": "電子工業",
    "14": "建材營造業", "15": "航運業", "16": "觀光餐旅", "17": "金融保險業",
    "18": "貿易百貨業", "19": "綜合", "20": "其他業", "21": "化學工業",
    "22": "生技醫療業", "23": "油電燃氣業", "24": "半導體業", "25": "電腦及週邊設備業",
    "26": "光電業", "27": "通信網路業", "28": "電子零組件業", "29": "電子通路業",
    "30": "資訊服務業", "31": "其他電子業", "32": "文化創意業", "33": "農業科技業",
    "34": "電子商務", "35": "綠能環保", "36": "數位雲端", "37": "運動休閒",
    "38": "居家生活",
}

class CompanyInfoFetcher:
    """從台灣證券交易所的開放資料抓取並處理所有公司的基本資料。"""

    def __init__(self):
        self.csv_list = [
            "https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv",  # 上市公司資料
            "https://mopsfin.twse.com.tw/opendata/t187ap03_O.csv",  # 上櫃公司資料
            "https://mopsfin.twse.com.tw/opendata/t187ap03_R.csv",  # 興櫃公司資料
        ]

    def _py(self, v):
        if v is None or pd.isna(v): return None
        if isinstance(v, str):
            v = v.strip()
            return v if v != "" else None
        if isinstance(v, np.integer): return int(v)
        if isinstance(v, np.floating): return float(v)
        return v

    def _to_int(self, value):
        try:
            v = self._py(value)
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _format_date(self, value):
        v = self._py(value)
        if not v: return None
        s = str(v)
        if len(s) == 8 and s.isdigit(): return f"{s[:4]}-{s[4:6]}-{s[6:]}"
        if '.' in s: s = s.split('.')[0] # 處理 '19620209.0' 這種情況
        if len(s) == 8 and s.isdigit(): return f"{s[:4]}-{s[4:6]}-{s[6:]}"
        return s
    
    def _parse_par_value(self, value):
        v = self._py(value)
        if not v: return None
        m = re.search(r"([\d.]+)", str(v))
        return float(m.group(1)) if m else None

    def _industry_name(self, value):
        v = self._py(value)
        if v is None: return None
        key = str(v).zfill(2)
        return INDUSTRY_CODE_MAP.get(key, "未知產業")

    def _build_company_dict(self, r: pd.Series) -> Dict[str, Any]:
        """建立單一公司的資料字典"""
        return {
            "name": self._py(r.get("公司名稱")),
            "code": self._py(r.get("公司代號")),
            "shortName": self._py(r.get("公司簡稱")),
            "website": self._py(r.get("網址")),
            "foundedDate": self._format_date(r.get("成立日期")),
            "gov": {
                "profile": {
                    "reportDate": self._py(r.get("出表日期")),
                    "companyCode": self._py(r.get("公司代號")),
                    "companyName": self._py(r.get("公司名稱")),
                    "companyShortName": self._py(r.get("公司簡稱")),
                    "foreignRegistrationCountry": self._py(r.get("外國企業註冊地國")),
                    "industry": {"code": self._py(r.get("產業別")), "name": self._industry_name(r.get("產業別"))},
                    "taxId": self._py(r.get("營利事業統一編號")),
                },
                "management": {
                    "chairman": self._py(r.get("董事長")),
                    "generalManager": self._py(r.get("總經理")),
                    "spokesperson": self._py(r.get("發言人")),
                    "spokespersonTitle": self._py(r.get("發言人職稱")),
                    "actingSpokesperson": self._py(r.get("代理發言人")),
                },
                "contact": {
                    "address": self._py(r.get("住址")), "phone": self._py(r.get("總機電話")),
                    "fax": self._py(r.get("傳真機號碼")), "email": self._py(r.get("電子郵件信箱")),
                    "website": self._py(r.get("網址")), "englishAddress": self._py(r.get("英文通訊地址")),
                },
                "listing": {
                    "listingDate": self._format_date(r.get("上市日期")),
                    "parValuePerShare": self._parse_par_value(r.get("普通股每股面額")),
                    "currency": "TWD",
                },
                            "capital": {
                                "paidInCapital": self._to_int(r.get("實收資本額")),
                                "issuedCommonShares": self._to_int(r.get("已發行普通股數或TDR原股發行股數")),
                                "privatePlacementShares": self._to_int(r.get("私募股數")),
                                "preferredShares": self._to_int(r.get("特別股")),
                            },
                            # "transferAgent": {
                            #     "name": self._py(r.get("股票過戶機構")), "phone": self._py(r.get("過戶電話")),
                            #     "address": self._py(r.get("過戶地址")),
                            # },
                            # "audit": {
                            #     "firm": self._py(r.get("簽證會計師事務所")),
                            #     "auditors": [self._py(a) for a in [r.get("簽證會計師1"), r.get("簽證會計師2")] if pd.notna(a)],
                            # },
                            "financialReportType": self._py(r.get("編制財務報表類型")),                "stockType": "COMMON",
            },
        }

    def fetch_all(self) -> Dict[str, Any]:
        """抓取所有公司的資料並以字典形式回傳"""
        logger.info("Fetching company info from TWSE open data...")
        dfs = []
        for url in self.csv_list:
            try:
                df = pd.read_csv(url, encoding="utf-8-sig")
                df["公司代號"] = df["公司代號"].astype(str).str.strip()
                df["公司名稱"] = df["公司名稱"].astype(str).str.strip()
                dfs.append(df)
            except Exception as e:
                logger.error(f"Failed to load data from {url}: {e}")
                continue

        if not dfs:
            logger.error("No data loaded, aborting.")
            return {}

        full_df = pd.concat(dfs, ignore_index=True)
        # 移除重複的股票代號，保留第一個出現的
        full_df = full_df.drop_duplicates(subset=["公司代號"], keep="first")
        
        all_companies_dict = {}
        for _, row in full_df.iterrows():
            code = self._py(row.get("公司代號"))
            if code and code.isdigit() and len(code) >= 4:
                try:
                    all_companies_dict[code] = self._build_company_dict(row)
                except Exception as e:
                    logger.warning(f"Could not process company {code}: {e}")
        
        logger.info(f"Fetched and processed {len(all_companies_dict)} companies.")
        return all_companies_dict
