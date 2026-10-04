"""
etf_utils.py — 主動式 ETF 爬蟲共用工具

提供：
  create_session()           建立帶重試機制的 Session
  clean_snapshot()           提取持股乾淨欄位
  write_holdings_update()    統一寫入 topHoldings / holdingsHistory
  record_unchanged_snapshot() 無更新時仍記錄當日快照
  write_github_output()      輸出逐筆 ETF 狀態到 GITHUB_OUTPUT
  pcf_to_holdings_date()     申購買回清單日期 → 持股所屬交易日
  today_tw()                 台北今天
"""

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HISTORY_KEEP_DAYS = 30

TWSE_HOLIDAY_URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"


def today_tw() -> str:
    """台北今天（CI 跑在 UTC，不能用 date.today()）。"""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).date().isoformat()


@lru_cache(maxsize=1)
def _tw_holidays() -> frozenset:
    """證交所 OpenAPI 休市表（當年）。名稱含「交易日」的是開始／最後交易日，那天有開盤，要排除。
    抓不到就回空集合，退化成只跳週末。"""
    try:
        rows = requests.get(TWSE_HOLIDAY_URL, timeout=15).json()
        out = set()
        for r in rows:
            roc = str(r.get("Date", ""))
            if len(roc) != 7 or "交易日" in r.get("Name", ""):
                continue
            out.add(f"{int(roc[:3]) + 1911}-{roc[3:5]}-{roc[5:]}")
        return frozenset(out)
    except Exception as e:                                     # noqa: BLE001
        print(f"  [WARN] 休市表取得失敗（{e}），只跳週末")
        return frozenset()


def prev_trading_day(iso: str) -> str:
    """iso 之前（不含）最近的台股交易日。"""
    d = date.fromisoformat(iso) - timedelta(days=1)
    holidays = _tw_holidays()
    while d.weekday() >= 5 or d.isoformat() in holidays:
        d -= timedelta(days=1)
    return d.isoformat()


def pcf_to_holdings_date(pub_date: Optional[str]) -> Optional[str]:
    """申購買回清單公告日 → 持股所屬交易日（往回一個交易日）。

    群益、台新官網的日期是「下一交易日的申購買回清單」，內容是前一交易日收盤後的持股：
    2026-10-01 收盤後兩家都標 10-02。其他投信標的是持股日本身。不換算的話同一份持股
    會比別家多一天，推播取全體眾數日期時會被拉偏；而且不能只在「日期 > 今天」才換——
    16:00 官網還掛著前一天公告的清單時，日期剛好等於今天，內容卻是昨天的持股。
    """
    if not pub_date:
        return pub_date
    return prev_trading_day(pub_date)


def create_session() -> requests.Session:
    """建立帶重試機制的 requests Session（GET + POST，retries=3）。"""
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.5,
        status_forcelist=[408, 429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


# 2026-10-01 起 CMoney GetDtnoData 要求驗證（不帶回 Error Code 101 Auth Failed）。
# CMoney ETF 網頁會在 SSR 裡發一組訪客 JWT（is_guest=true，約一天有效），
# 任何打開網頁的人都拿得到；每次執行先取一組帶上即可，不需要帳號。
# 主動型（每日）與被動型（週更）爬蟲共用，避免只修一支。
CMONEY_TOKEN_PAGE = "https://www.cmoney.tw/etf/tw/00981A/fundholding"
_CMONEY_GUEST_TOKEN_RE = re.compile(r'tokens:\{at:"([^"]+)"')
_cmoney_guest_token: Optional[str] = None


def cmoney_guest_token(session: requests.Session, headers: dict) -> str:
    global _cmoney_guest_token
    if _cmoney_guest_token is None:
        try:
            page = session.get(CMONEY_TOKEN_PAGE, headers=headers, timeout=30).text
            m = _CMONEY_GUEST_TOKEN_RE.search(page)
            _cmoney_guest_token = m.group(1) if m else ""
            if not _cmoney_guest_token:
                print("[WARN] CMoney 網頁找不到訪客 token，API 多半會回 Auth Failed")
        except Exception as e:                                 # noqa: BLE001
            print(f"[WARN] 取得 CMoney 訪客 token 失敗：{e}")
            _cmoney_guest_token = ""
    return _cmoney_guest_token


def clean_snapshot(h: dict, *, has_foreign_code: bool = False) -> dict:
    """
    從持股 dict 提取乾淨欄位（不含 previousWeight 等比較性欄位）。
    has_foreign_code: 保留 foreignCode 欄位（US/JP 個股用）。
    """
    result: dict = {"name": h["name"], "weight": h["weight"]}
    if h.get("shares") is not None:
        result["shares"] = h["shares"]
    if h.get("code"):
        result["code"] = h["code"]
    if has_foreign_code and h.get("foreignCode"):
        result["foreignCode"] = h["foreignCode"]
    return result


def record_unchanged_snapshot(
    json_path: Path,
    data: dict,
    etf_code: str,
    holdings_clean: List[dict],
    source_date: str,
) -> bool:
    """
    當來源網站尚未發佈新資料（source_date == lastUpdated），
    仍以今天的執行日期寫一筆相同快照到 holdingsHistory，
    讓歷史記錄連續、不留空白交易日。
    topHoldings 與 lastUpdated 不更動。
    """
    today = today_tw()
    if "holdingsHistory" not in data:
        data["holdingsHistory"] = {}

    if today != source_date and today not in data["holdingsHistory"]:
        data["holdingsHistory"][today] = holdings_clean
        for old_d in sorted(data["holdingsHistory"].keys(), reverse=True)[HISTORY_KEEP_DAYS:]:
            del data["holdingsHistory"][old_d]
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"  [OK] {etf_code} 來源未更新，記錄 {today} 快照（持股同 {source_date}）")
    else:
        print(f"  [SKIP] {etf_code} 數據無變化（{source_date}），今日已有記錄")
    return "unchanged"


def write_holdings_update(
    json_path: Path,
    etf_code: str,
    holdings: list,
    tran_date: Optional[str],
    *,
    has_foreign_code: bool = False,
    history_only: bool = False,
) -> bool:
    """
    更新 ETF JSON 的 topHoldings 與 holdingsHistory。

    has_foreign_code: 持股含 foreignCode 欄位（US/JP 個股）。
    history_only:     只補 holdingsHistory，不動 topHoldings / lastUpdated（backfill 用）。
    """
    if not json_path.exists():
        print(f"  [WARN] 找不到 {json_path.name}")
        return False

    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        if not tran_date:
            print(f"  [SKIP] 無法取得資料日期，跳過寫入")
            return False

        # 保險：持股不可能屬於未來。新接的官網若也標申購買回清單日，在這裡被攔下並告警，
        # 該在爬蟲裡改用 pcf_to_holdings_date()。
        today = today_tw()
        if tran_date > today:
            fixed = min(prev_trading_day(tran_date), today)
            print(f"  [WARN] {etf_code} 來源日期 {tran_date} 晚於今天，改記為 {fixed}"
                  f"（若此來源是申購買回清單日，爬蟲應改用 pcf_to_holdings_date）")
            tran_date = fixed

        if "holdingsHistory" not in data:
            data["holdingsHistory"] = {}

        def _snap(h: dict) -> dict:
            return clean_snapshot(h, has_foreign_code=has_foreign_code)

        def _key(h: dict) -> str:
            if has_foreign_code:
                return h.get("code") or h.get("foreignCode") or h.get("name", "")
            return h.get("code") or h.get("name", "")

        # 日期不倒退：分層補資料後，同一檔可能先被 A 來源寫到 D，再被 B 來源拿到 D-1。
        # 舊的那份只補進 history（若還沒有），不動 topHoldings／lastUpdated，
        # 否則畫面會從今天退回昨天，加減碼也會拿錯基準。
        prev_date = data.get("lastUpdated")
        if not history_only and prev_date and tran_date < prev_date:
            print(f"  [SKIP] {etf_code} 來源日期 {tran_date} 早於現有 {prev_date}，只補歷史")
            history_only = True

        if history_only:
            if tran_date in data["holdingsHistory"]:
                print(f"  [SKIP] {tran_date} 已存在")
                return True
            data["holdingsHistory"][tran_date] = [_snap(h) for h in holdings]
        else:
            prev_holdings = data.get("topHoldings", [])
            prev_date = data.get("lastUpdated")

            if tran_date == prev_date and prev_holdings:
                if sorted([_snap(h) for h in holdings], key=_key) == \
                        sorted([_snap(h) for h in prev_holdings], key=_key):
                    return record_unchanged_snapshot(
                        json_path, data, etf_code, [_snap(h) for h in holdings], tran_date
                    )

            if prev_date and prev_holdings and prev_date not in data["holdingsHistory"]:
                data["holdingsHistory"][prev_date] = [_snap(h) for h in prev_holdings]

            prev_map = {_key(h): h for h in prev_holdings}
            for h in holdings:
                prev = prev_map.get(_key(h))
                if prev:
                    prev_w = prev.get("weight", 0)
                    h["previousWeight"] = prev_w
                    h["weightChange"] = round(h["weight"] - prev_w, 2)
                    prev_s = prev.get("shares") or 0
                    h["previousShares"] = prev_s
                    h["sharesChange"] = (
                        (h["shares"] - prev_s) if h.get("shares") is not None and prev_s else 0
                    )
                else:
                    h["previousWeight"] = 0
                    h["weightChange"] = h["weight"]
                    h["previousShares"] = 0
                    h["sharesChange"] = h.get("shares") or 0

            data["topHoldings"] = holdings
            data["holdingsHistory"][tran_date] = [_snap(h) for h in holdings]
            data["lastUpdated"] = tran_date

        for old_d in sorted(data["holdingsHistory"].keys(), reverse=True)[HISTORY_KEEP_DAYS:]:
            del data["holdingsHistory"][old_d]

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"  [OK] {etf_code} — {len(holdings)} 筆持股，{tran_date}")
        return True

    except Exception as e:
        print(f"  [ERROR] 寫入失敗: {e}")
        import traceback
        traceback.print_exc()
        return False


def write_github_output(results: Dict[str, Tuple[str, str]]) -> None:
    """
    輸出逐筆 ETF 狀態到 GITHUB_OUTPUT。
    results: {etf_code: (status, date)}
      status: "updated" | "unchanged" | "failed"
      date:   "YYYY-MM-DD" 或 ""
    """
    gho = os.environ.get("GITHUB_OUTPUT")
    if not gho:
        return
    with open(gho, "a", encoding="utf-8") as f:
        for code, (status, tran_date) in results.items():
            f.write(f"ETF_{code}_STATUS={status}\n")
            f.write(f"ETF_{code}_DATE={tran_date}\n")
