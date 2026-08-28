"""
FinMind API v4 共用取數。

**為什麼集中在這裡**：2026-08-26 證交所來函後，盤後統計陸續從 www.twse.com.tw
改抓 FinMind（見 docs 與各 fetcher 的模組註解）。多個 domain 都要打同一支 API，
token 讀取、重試、錯誤語意集中一處，才不會每個檔案各寫一套。

token 由環境變數提供，優先序 FINMIND_API_TOKENS（CI）→ FINMIND_API_TOKEN_local（本機）。
免 token 也能打，但只吃得到 register 等級——**全市場查詢（不帶 data_id）需要付費層**，
沒 token 會整批回 400 而不是回少一點資料，屬於「上線才發現」的那種壞法。
"""

import logging
import os
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

DATA_URL = "https://api.finmindtrade.com/api/v4/data"
TIMEOUT_SEC = 120  # 全市場單日可達十餘萬筆（含權證），30 秒會逾時


def finmind_token() -> str:
    """回傳第一組可用 token（環境變數允許逗號分隔多組，取第一組）。"""
    raw = (
        os.environ.get("FINMIND_API_TOKENS")
        or os.environ.get("FINMIND_API_TOKEN_local")
        or ""
    )
    return raw.split(",")[0].strip()


def fetch_finmind(
    dataset: str,
    start_date: str,
    end_date: str,
    *,
    data_id: Optional[str] = None,
    label: Optional[str] = None,
    retries: int = 3,
    retry_delay: int = 10,
    quiet: bool = False,
) -> Optional[List[Dict]]:
    """
    查 FinMind 一段區間的資料。

    Args:
        start_date / end_date: YYYY-MM-DD。單日就把兩者設成同一天——FinMind 只回那天，
            非交易日或尚未公布時回空，呼叫端不會拿到前一日的資料卻標成當天。
        data_id: 個股代號；不帶則查全市場（需付費層）。
        quiet: 逐日探測「哪一天有資料」時設 True——查不到是預期結果，
            用 ERROR 記錄會讓正常的往回找變成一串假警報。

    Returns:
        rows；全部嘗試都失敗或無資料時回 None（呼叫端據此保留既有檔案，不要寫入空值）。
    """
    name = label or dataset
    params = {"dataset": dataset, "start_date": start_date, "end_date": end_date}
    if data_id:
        params["data_id"] = data_id

    headers = {"Accept": "application/json"}
    token = finmind_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        logger.warning("FinMind %s：未設定 token，全市場查詢會被擋在 register 等級", name)

    for attempt in range(retries):
        try:
            resp = requests.get(DATA_URL, params=params, headers=headers, timeout=TIMEOUT_SEC)
            if resp.status_code == 400:
                # 等級不足與參數錯誤都回 400，訊息要留著否則沒人知道是哪一種
                logger.error("FinMind %s 400: %s", name, resp.text[:200])
                return None
            resp.raise_for_status()
            rows = (resp.json() or {}).get("data") or []
            if rows:
                return rows
            if not quiet:
                logger.warning(
                    "FinMind %s 無資料 %s~%s (attempt %d/%d)", name, start_date, end_date, attempt + 1, retries
                )
        except Exception:
            logger.warning(
                "FinMind %s 取得失敗 %s~%s (attempt %d/%d)", name, start_date, end_date,
                attempt + 1, retries, exc_info=True,
            )
        if attempt < retries - 1:
            time.sleep(retry_delay)

    if not quiet:
        logger.error("FinMind %s 取得失敗 %s~%s", name, start_date, end_date)
    return None
