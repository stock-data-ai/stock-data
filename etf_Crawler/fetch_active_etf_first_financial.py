# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
# ]
# ///
"""
fetch_active_etf_first_financial.py

從第一金投信官網 (fsitc.com.tw) 爬取主動型 ETF 每日持股明細，
更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

支援 ETF：
  00994A  主動第一金台股優 (Internal ID: 182)
"""

import json
import sys
import time
from datetime import date, datetime
from etf_utils import create_session, write_github_output, write_holdings_update
from pathlib import Path
from typing import Optional, List, Dict, Any

try:
    import requests
except ImportError:
    print("缺少依賴，請先執行：uv add requests")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

API_URL = "https://www.fsitc.com.tw/WebAPI.aspx/Get_hd"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json; charset=utf-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

# 第一金投信主動型 ETF 代號與內部 ID 映射
FIRST_FINANCIAL_ACTIVE_ETFS = {
    "00994A": "182",  # 主動第一金台股優
}

session = create_session()


def fetch_holdings(etf_code: str) -> tuple:
    """
    回傳 (holdings, tran_date_str)
    holdings: [{"name": ..., "weight": ..., "code": ..., "shares": ...}, ...]
    tran_date_str: "2026-04-30" 或 None
    """
    fund_id = FIRST_FINANCIAL_ACTIVE_ETFS.get(etf_code)
    if not fund_id:
        print(f"  [ERROR] 未知的 ETF 代號: {etf_code}")
        return [], None

    print(f"  抓取 {etf_code} (ID: {fund_id}) 的持股資料...")

    payload = {
        "pStrFundID": fund_id,
        "pStrDate": ""  # 空字串表示抓取最新
    }

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.post(API_URL, json=payload, headers=HEADERS, timeout=45)
            resp.raise_for_status()

            result = resp.json()
            data_str = result.get("d")
            if not data_str:
                print(f"  [WARN] API 未回傳資料內容（第 {attempt}/{max_attempts} 次）")
                if attempt < max_attempts:
                    time.sleep(attempt * 10)
                    continue
                return [], None

            raw_holdings = json.loads(data_str)
            if not raw_holdings:
                print(f"  [WARN] 持股清單為空（第 {attempt}/{max_attempts} 次）")
                if attempt < max_attempts:
                    time.sleep(attempt * 10)
                    continue
                return [], None

            holdings = []
            tran_date = None

            for item in raw_holdings:
                if item.get("group") != "1":
                    continue

                if not tran_date:
                    tran_date = item.get("sdate")

                code = item.get("A", "").strip()
                name = item.get("B", "").strip().replace(" ", "")
                weight_str = item.get("C", "0")
                shares_str = item.get("D", "0").replace(",", "")

                try:
                    weight = round(float(weight_str), 2)
                    shares = int(float(shares_str))
                except (ValueError, TypeError):
                    continue

                if weight <= 0 and shares <= 0:
                    continue

                entry = {
                    "name": name,
                    "weight": weight,
                    "shares": shares if shares > 0 else None,
                }

                if code.isdigit() and 4 <= len(code) <= 6:
                    entry["code"] = code
                elif code:
                    entry["foreignCode"] = code

                holdings.append(entry)

            return holdings, tran_date

        except Exception as e:
            print(f"  [ERROR] 抓取失敗: {e}（第 {attempt}/{max_attempts} 次）")
            if attempt < max_attempts:
                time.sleep(attempt * 10)

    return [], None

def update_etf_json(etf_code: str, holdings: list, tran_date: Optional[str]) -> bool:
    return write_holdings_update(
        ETF_DATA_DIR / f"{etf_code}.json",
        etf_code, holdings, tran_date,
        has_foreign_code=True,
    )



def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(FIRST_FINANCIAL_ACTIVE_ETFS.keys())
    
    success, failed, unchanged = 0, [], 0
    results: dict = {}

    for i, etf_code in enumerate(targets):
        if etf_code not in FIRST_FINANCIAL_ACTIVE_ETFS:
            print(f"  [SKIP] 不支援的 ETF：{etf_code}")
            continue

        print(f"\n[{i+1}/{len(targets)}] {etf_code}")

        holdings, tran_date = fetch_holdings(etf_code)
        if holdings:
            result = update_etf_json(etf_code, holdings, tran_date)
            if result is True:
                success += 1
                results[etf_code] = ("updated", tran_date or "")
            elif result == "unchanged":
                unchanged += 1
                results[etf_code] = ("unchanged", tran_date or "")
            else:
                failed.append(etf_code)
                results[etf_code] = ("failed", "")
        else:
            failed.append(etf_code)
            results[etf_code] = ("failed", "")

        if i < len(targets) - 1:
            time.sleep(1)

    write_github_output(results)
    print(f"\n第一金投信主動 ETF 更新完成 — 已更新: {success}，無變化: {unchanged}，失敗: {len(failed)}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
