"""
【一次性】回補外資持股張數（`institutionalInvestors[date].foreign_shares`）。

**為什麼需要**：`foreign_shares` 是 2026-09-04 才加進每日流程的，在那之前的日期都沒有。
籌碼總覽的「外資持股」軌因此只有一個資料點，畫出來是一條貼在最右邊的短線，
右軸三個刻度全都一樣（退化刻度），看起來像壞掉而不是「沒有歷史」。

**張數是官方揭露值**（FinMind `ForeignInvestmentShares`），不是用持股比率乘發行股數
回推的——發行股數來源不一致會讓下游跟著錯，而且錯得很安靜。

**只補、不建**：僅對「已經有 institutionalInvestors 紀錄」的日期補上 `foreign_shares`。
自己建一筆新紀錄會缺 foreign_buy/sell 等欄位，消費端做 `rec.foreign_buy - rec.foreign_sell`
會得到 NaN，比沒有資料更糟。

批次路徑的理由與 securities_lending 相同：逐日呼叫會變成日數 × 公司數 次檔案 IO。
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List

from finance_tools.core import DataProcessor, FileManager
from finance_tools.core.timezone import now_tw
from finance_tools.utils.company_list_loader import load_companies_for_processing
from finance_tools.utils.finmind import fetch_finmind

logger = logging.getLogger(__name__)

DATASET = "TaiwanStockShareholding"
SHARES_PER_LOT = 1000


def _weekdays_between(start_str: str, end_str: str) -> List[str]:
    start = datetime.strptime(start_str, "%Y%m%d")
    end = datetime.strptime(end_str, "%Y%m%d")
    days, cur = [], start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return days


def run_backfill_foreign_shares(args):
    """回補外資持股張數。`--backfill-from YYYYMMDD`，預設補到昨天。"""
    file_mgr = FileManager()
    processor = DataProcessor()

    companies = load_companies_for_processing(args, file_mgr)
    company_codes = {c["code"] for c in companies} if companies else None

    start = (getattr(args, "backfill_from", None) or "").replace("-", "")
    if not start:
        logger.error("必須指定 --backfill-from YYYYMMDD")
        return
    now = now_tw()
    end = (now - timedelta(days=1) if now.hour < 18 else now).strftime("%Y%m%d")
    dates = _weekdays_between(start, end)
    logger.info(f"回補外資持股張數：{start} ～ {end}，共 {len(dates)} 個交易日。")

    # 先把整段抓進記憶體，再對每家公司只讀寫一次
    by_code: Dict[str, Dict[str, int]] = {}
    got = 0
    for d in dates:
        iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        # TaiwanStockShareholding 帶 start≠end 會回空，只能逐日查（見 twse_shareholding_fetcher）
        rows = fetch_finmind(DATASET, iso, iso, label="外資持股張數", retries=1, retry_delay=0, quiet=True)
        if not rows:
            continue
        got += 1
        for r in rows:
            code = str(r.get("stock_id", "")).strip()
            raw = r.get("ForeignInvestmentShares")
            if not code or raw is None:
                continue
            if company_codes and code not in company_codes:
                continue
            try:
                by_code.setdefault(code, {})[iso] = int(round(float(raw) / SHARES_PER_LOT))
            except (TypeError, ValueError):
                continue
        time.sleep(1)

    logger.info(f"取數完成：{got}/{len(dates)} 個交易日有資料，涵蓋 {len(by_code)} 家公司。開始寫檔。")

    written = 0
    for code, per_date in by_code.items():
        existing = file_mgr.load_financial_data(code)
        if not existing:
            continue
        inst = existing.get("historical", {}).get("institutionalInvestors")
        if not inst:
            continue
        touched = False
        for iso, lots in per_date.items():
            rec = inst.get(iso)
            if rec is None:          # 只補、不建：見模組註解
                continue
            if rec.get("foreign_shares") != lots:
                rec["foreign_shares"] = lots
                touched = True
        if touched and file_mgr.save_financial_data(code, processor.clean_nan(existing)):
            written += 1

    logger.info(f"回補完成，總計 {written} 家公司。")
