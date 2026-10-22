"""財報類資料的全市場抓取：每一期發一次請求，拿回全部公司，再依代號分組。

取代舊的「每家公司各打一次 API、每家之間 sleep 1～3 秒」。抓取失敗（額度用盡、
非成功回應、重試後仍連不上）一律往外丟例外，讓整個任務停下來：
**抓不完整就不寫任何檔**，不拿缺了幾季的資料去合併。
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Callable, Dict, Iterable, List, Optional

import pandas as pd

import finance_tools.config as config
from finance_tools.core.data_processor import normalize_item_name

logger = logging.getLogger(__name__)

_QUARTER_ENDS = ("03-31", "06-30", "09-30", "12-31")


def quarter_end_dates(years_back: int, today: date) -> List[str]:
    """從 (今年 - years_back) 的 Q1 到今天為止的每個季末日。"""
    out = []
    for year in range(today.year - years_back, today.year + 1):
        for md in _QUARTER_ENDS:
            d = f"{year}-{md}"
            if d <= today.isoformat():
                out.append(d)
    return out


def month_start_dates(years_back: int, today: date) -> List[str]:
    """從 (今年 - years_back) 的 1 月到今天為止的每月 1 日。

    月營收資料以「公布月的 1 日」為日期：2026-10-01 那天拿到的是 9 月營收。
    """
    out = []
    for year in range(today.year - years_back, today.year + 1):
        for month in range(1, 13):
            d = f"{year}-{month:02d}-01"
            if d <= today.isoformat():
                out.append(d)
    return out


def item_filter(items: Dict[str, List[str]]) -> Callable[[pd.DataFrame], pd.DataFrame]:
    """只留下 `items` 裡列出的科目（比對前先正規化括號），並丟掉百分比列。

    資產負債表一季約 186k 列，留下需要的科目後剩一成左右；
    在每一期抓回來時就過濾，七年 28 季才不會全部堆在記憶體裡。
    """
    wanted = {normalize_item_name(n) for names in items.values() for n in names}

    def _keep(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = df[~df["type"].astype(str).str.endswith("_per")] if "type" in df.columns else df
        return df[df["origin_name"].map(normalize_item_name).isin(wanted)]

    return _keep


def fetch_market(
    client,
    dataset: str,
    dates: Iterable[str],
    keep: Optional[Callable[[pd.DataFrame], pd.DataFrame]] = None,
) -> pd.DataFrame:
    """抓 `dataset` 在每個 `dates` 的全市場資料並合併。任何一期失敗就整個丟例外。"""
    dates = list(dates)

    def _one(d: str) -> pd.DataFrame:
        df = client.fetch_market_day(dataset, d)
        return keep(df) if keep is not None else df

    with ThreadPoolExecutor(max_workers=config.MARKET_FETCH_WORKERS) as pool:
        frames = list(pool.map(_one, dates))

    frames = [f for f in frames if not f.empty]
    merged = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    stocks = merged["stock_id"].nunique() if not merged.empty else 0
    logger.info(f"  {dataset}: {len(dates)} 期、{len(merged)} 列、{stocks} 家")
    return merged


def split_by_stock(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """全市場資料依 stock_id 分組；沒有資料的公司不在結果裡（呼叫端視為空）。"""
    if df.empty:
        return {}
    return {str(code): g.reset_index(drop=True) for code, g in df.groupby("stock_id")}
