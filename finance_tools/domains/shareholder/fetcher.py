import requests
from io import StringIO
import pandas as pd
from typing import Dict, List, Any
import logging

logger = logging.getLogger(__name__)

TDCC_API_URL = "https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5"

# 持股分級對照表 (對齊現有 schema)
TDCC_RANGE_MAP = {
    1: "1-999",
    2: "1,000-5,000",
    3: "5,001-10,000",
    4: "10,001-15,000",
    5: "15,001-20,000",
    6: "20,001-30,000",
    7: "30,001-40,000",
    8: "40,001-50,000",
    9: "50,001-100,000",
    10: "100,001-200,000",
    11: "200,001-400,000",
    12: "400,001-600,000",
    13: "600,001-800,000",
    14: "800,001-1,000,000",
    15: "1,000,001以上",
    16: "合計",
}

def fetch_all_tdcc_shareholding_via_api() -> Dict[str, List[Dict[str, Any]]]:
    """
    透過 TDCC 開放資料 API 抓取全市場最新的股權分散表。
    回傳格式為 { stock_id: [records] }
    """
    logger.info(f"[TDCC API] 正在從 {TDCC_API_URL} 抓取全市場最新資料...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        # 下載全市場資料
        response = requests.get(TDCC_API_URL, headers=headers, timeout=60)
        response.raise_for_status()
        
        # 讀取 CSV (TDCC API 回傳的是 CSV 格式)
        df = pd.read_csv(StringIO(response.text), encoding="utf-8")
        
        # 欄位重新命名以對齊現有格式
        df = df.rename(columns={
            "資料日期": "data_date",
            "證券代號": "stock_id",
            "持股分級": "holding_index",
            "人數": "holder_count",
            "股數": "shares",
            "占集保庫存數比例%": "ratio_pct"
        })

        # 轉換資料型別與清理
        df["data_date"] = df["data_date"].astype(str)
        df["stock_id"] = df["stock_id"].astype(str).str.strip()
        
        # 將持股分級索引轉為文字描述
        df["holding_range"] = df["holding_index"].map(TDCC_RANGE_MAP)
        
        # 過濾掉無法對應的分級 (如 17 差異)
        df = df[df["holding_range"].notna()].copy()
        
        # 依照 stock_id 分群
        results = {}
        for stock_id, group in df.groupby("stock_id"):
            group = group.copy()
            group["序"] = group["holding_index"].astype(int)
            
            # 轉換為 dict list，並移除不需要存入個別日期的冗餘欄位
            records = group.to_dict(orient="records")
            results[stock_id] = records
            
        logger.info(f"[TDCC API] 成功獲取 {len(results)} 檔標的的資料")
        return results

    except Exception as e:
        logger.error(f"[TDCC API] 抓取失敗: {e}")
        return {}
