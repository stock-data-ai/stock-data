"""
【定期】ETF 配息紀錄：src/data/etf/dividends/{code}.json（ETF 頁「配息」分頁、AI 助理讀這份）。

資料來源：FinMind TaiwanStockDividendResult（除權息結果）。
2026-11-04 前這份檔由已刪除的手動腳本 fetch_etf_data.py 爬玩股網產生，2026-05-07 後沒人跑、
也沒有任何排程或檢查負責，停更半年沒人發現；改用已在用的 FinMind，排進週六股利班，
另由 health-check.yml 看 lastUpdated。

欄位沿用舊檔（前端 ETFDividendData，已上架 App 也在讀，只能相容擴充）：
  date         除息日
  cashDividend 每單位現金配息（元）
  cashYield    配息 ÷ 除息前一日收盤 × 100（舊檔同一算法，2026-04-23 0056：1 ÷ 42.61 = 2.346867）
  price        除息前一日收盤（FinMind before_price；ETF 頁標「除息日股價」）
  period       除息日的西元年

合併規則：舊紀錄全部保留，同一個除息日以 FinMind 為準；查詢失敗不動檔案。
lastUpdated 是「這次確認過的日期」，健康檢查拿它判斷這份資料有沒有在更新。
"""
import argparse
import glob
import json
import logging
import os
import sys
import time
from datetime import date, timedelta
from typing import Dict, List, Optional

import requests

from finance_tools.core.timezone import today_str
from finance_tools.utils.finmind import DATA_URL, TIMEOUT_SEC, finmind_token

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
ETF_DIR = os.path.join(REPO_ROOT, "src", "data", "etf")
DIV_DIR = os.path.join(ETF_DIR, "dividends")
DATASET = "TaiwanStockDividendResult"
# 已有檔案：從最後一次除息往前多抓這麼多天（FinMind 偶爾補登、更正）
OVERLAP_DAYS = 120
# 新 ETF（還沒有配息檔）：從這天抓起
FIRST_START = "2005-01-01"
# 失敗比例超過這個就以錯誤結束，讓 workflow 變紅燈、健康檢查寄信
MAX_FAIL_RATIO = 0.2


class FetchError(Exception):
    """FinMind 連線或伺服器錯誤（不是「這段期間沒有配息」）。"""


def fetch_rows(code: str, start: str, end: str, retries: int = 3) -> List[Dict]:
    """
    查一檔 ETF 的除權息結果。**沒有資料回 []，查詢失敗丟 FetchError**——兩者要分開：
    共用的 fetch_finmind 兩種都回 None，拿來用的話失敗會被當成「沒有新配息」、lastUpdated 照樣更新，
    健康檢查就看不出停更；而且它對「沒資料」也重試 3 次各等 10 秒，三百多檔要跑兩小時。
    """
    headers = {"Accept": "application/json"}
    token = finmind_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = {"dataset": DATASET, "data_id": code, "start_date": start, "end_date": end}
    last_err = "unknown"
    for attempt in range(retries):
        try:
            resp = requests.get(DATA_URL, params=params, headers=headers, timeout=TIMEOUT_SEC)
            if resp.status_code == 200:
                body = resp.json() or {}
                if body.get("status", 200) == 200:
                    return body.get("data") or []
                last_err = f"status {body.get('status')}: {str(body.get('msg'))[:120]}"
            else:
                last_err = f"HTTP {resp.status_code}: {resp.text[:120]}"
                if resp.status_code == 400:
                    break  # 等級不足或參數錯，重試沒用
        except requests.RequestException as err:
            last_err = str(err)[:160]
        if attempt < retries - 1:
            time.sleep(5 * (attempt + 1))
    raise FetchError(last_err)


def _etf_codes() -> List[str]:
    """ETF 清單：基本資料檔（src/data/etf/{code}.json）與既有配息檔的聯集。"""
    detail = {os.path.basename(p)[:-5] for p in glob.glob(os.path.join(ETF_DIR, "*.json"))}
    detail.discard("index")
    div = {os.path.basename(p)[:-5] for p in glob.glob(os.path.join(DIV_DIR, "*.json"))}
    return sorted(detail | div)


def _load(path: str) -> Optional[Dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def to_entry(row: Dict) -> Optional[Dict]:
    """FinMind 一列 → 舊檔格式。只收現金配息（「息」「權息」）；沒有除息前價格就不算殖利率。"""
    kind = str(row.get("stock_or_cache_dividend") or "")
    cash = row.get("stock_and_cache_dividend")
    before = row.get("before_price")
    day = str(row.get("date") or "")[:10]
    if "息" not in kind or not day or not isinstance(cash, (int, float)) or cash <= 0:
        return None
    price = before if isinstance(before, (int, float)) and before > 0 else None
    return {
        "date": day,
        "cashDividend": int(cash) if float(cash).is_integer() else round(float(cash), 6),
        "cashYield": round(cash / price * 100, 6) if price else None,
        "price": price,
        "period": int(day[:4]),
    }


def _same(a: Dict, b: Dict) -> bool:
    """金額與價格相同就算同一筆（不比殖利率尾數，避免每週改動一堆檔案）。"""
    def close(x, y):
        return (x is None and y is None) or (
            isinstance(x, (int, float)) and isinstance(y, (int, float)) and abs(x - y) < 1e-6)
    return close(a.get("cashDividend"), b.get("cashDividend")) and close(a.get("price"), b.get("price"))


def merge_dividends(existing: List[Dict], fresh: List[Dict]) -> List[Dict]:
    """舊紀錄全保留；同一除息日金額或價格不同才以新資料為準（相同就原樣保留）；新到舊排序。"""
    by_date = {e.get("date"): e for e in existing if e.get("date")}
    for e in fresh:
        old = by_date.get(e["date"])
        if old is None or not _same(old, e):
            by_date[e["date"]] = e
    return sorted(by_date.values(), key=lambda e: e["date"], reverse=True)


def update_one(code: str, today: str) -> str:
    """回傳 updated／unchanged／skipped（非標準格式、或沒配過息的新 ETF）／failed。"""
    path = os.path.join(DIV_DIR, f"{code}.json")
    doc = _load(path)
    if doc is not None and "dividends" not in doc:
        logger.warning("%s 配息檔不是標準格式（%s），不動它", code, ",".join(sorted(doc)))
        return "skipped"
    # 除息日在未來的是錯資料（舊玩股網檔 00921 有一筆 2031-03-10）：丟掉，也不能拿來算查詢起點——
    # 否則起點被推到 2030 年，真正的新配息全部漏抓（11/4 實測漏了 00921 的 6 月、9 月兩次）
    existing = [e for e in (doc or {}).get("dividends") or [] if (e.get("date") or "") <= today]
    last = max((e.get("date") or "" for e in existing), default="")
    start = (date.fromisoformat(last) - timedelta(days=OVERLAP_DAYS)).isoformat() if last else FIRST_START
    try:
        rows = fetch_rows(code, start, today)
    except FetchError as err:
        logger.error("%s 配息查詢失敗：%s", code, err)
        return "failed"  # 不動檔案：lastUpdated 維持舊日期，健康檢查看得到
    fresh = [e for e in (to_entry(r) for r in rows) if e]
    if doc is None and not fresh:
        return "skipped"  # 新 ETF 還沒配過息：不建空檔
    merged = merge_dividends(existing, fresh)
    out = {
        "stockNo": code,
        "listingDate": (doc or {}).get("listingDate"),
        "lastUpdated": today,
        "dividends": merged,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return "updated" if merged != (doc or {}).get("dividends") else "unchanged"


def run_update_etf_dividends(args: argparse.Namespace) -> None:
    today = today_str()
    codes = _etf_codes()
    if getattr(args, "code", None):
        codes = [args.code]
    if getattr(args, "limit", None):
        codes = codes[: args.limit]
    if not finmind_token():
        logger.warning("沒有 FinMind token：只吃得到 register 等級的額度")
    os.makedirs(DIV_DIR, exist_ok=True)
    counts = {"updated": 0, "unchanged": 0, "skipped": 0, "failed": 0}
    for i, code in enumerate(codes, 1):
        try:
            status = update_one(code, today)
        except Exception as err:  # 單檔壞掉不擋整批
            logger.error("%s 配息更新失敗：%s", code, err)
            status = "failed"
        counts[status] += 1
        if status == "updated":
            print(f"[{i}/{len(codes)}] {code} 有新配息", flush=True)
        time.sleep(0.2)
    print(f"OK ETF dividend update: {counts['updated']} 更新, {counts['unchanged']} 無新配息, "
          f"{counts['skipped']} 略過, {counts['failed']} 失敗（共 {len(codes)} 檔）")
    if codes and counts["failed"] / len(codes) > MAX_FAIL_RATIO:
        print(f"::error::ETF 配息更新失敗 {counts['failed']} 檔，超過 {int(MAX_FAIL_RATIO * 100)}%")
        sys.exit(1)
