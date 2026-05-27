# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
# ]
# ///
"""
fetch_active_etf_cathay.py

從國泰投信官網 API 爬取主動型 ETF 每日持股明細，
更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

支援 ETF：
  00400A (fundCode=EA) 國泰台股動能高息主動式ETF基金

API：GET https://cwapi.cathaysite.com.tw/api/ETF/GetIndexStockWeights?fundCode={fundCode}

用法：
    uv run etf_Crawler/fetch_active_etf_cathay.py                   # 更新全部
    uv run etf_Crawler/fetch_active_etf_cathay.py 00400A            # 更新指定
"""

import json
import sys
import time
from datetime import date
from etf_utils import create_session, write_holdings_update
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("缺少依賴，請先執行：uv add requests")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

API_BASE = "https://cwapi.cathaysite.com.tw/api/ETF/GetIndexStockWeights"
BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.cathaysite.com.tw/",
}

# 國泰投信主動型 ETF 對照表：ETF代號 → fundCode
CATHAY_ACTIVE_ETFS = {
    "00400A": "EA",  # 國泰台股動能高息主動式ETF基金
}

session = create_session()


def _clean_name(name: str) -> str:
    """去除股票名稱中的多餘空白。"""
    return " ".join(name.split())

def fetch_holdings(etf_code: str, fund_code: str) -> tuple:
    """
    回傳 (holdings, date_str)
    holdings: [{"name": ..., "weight": ..., "code": ...}, ...]
    date_str: "2026-05-12" 或 None
    """
    url = f"{API_BASE}?fundCode={fund_code}"
    print(f"  抓取 {url}")

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(url, headers=BASE_HEADERS, timeout=45)
            resp.raise_for_status()
            data = resp.json()

            if not data.get("success"):
                print(f"  [WARN] API 回應失敗: {data.get('returnMessage')}（第 {attempt}/{max_attempts} 次）")
                if attempt < max_attempts:
                    time.sleep(attempt * 10)
                    continue
                return [], None

            result = data.get("result", {})
            stocks = result.get("stockWeights", [])
            if not stocks:
                print(f"  [WARN] 無持股資料（第 {attempt}/{max_attempts} 次）")
                if attempt < max_attempts:
                    time.sleep(attempt * 10)
                    continue
                return [], None

            raw_date = result.get("date", "")
            tran_date = raw_date.replace("/", "-") if raw_date else None

            holdings = []
            for s in stocks:
                code_raw = str(s.get("stockCode", "")).strip()
                name = _clean_name(str(s.get("stockName", "")))
                weight_raw = s.get("weights")

                if weight_raw is None:
                    continue
                try:
                    weight = round(float(weight_raw), 2)
                except (ValueError, TypeError):
                    continue
                if weight <= 0:
                    continue

                entry: dict = {"name": name, "weight": weight}
                if code_raw.isdigit() and 4 <= len(code_raw) <= 6:
                    entry["code"] = code_raw

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
    )



def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(CATHAY_ACTIVE_ETFS.keys())
    unknown = [t for t in targets if t not in CATHAY_ACTIVE_ETFS]
    if unknown:
        print(f"[ERROR] 不支援的 ETF：{', '.join(unknown)}")
        print(f"支援清單：{', '.join(CATHAY_ACTIVE_ETFS.keys())}")
        sys.exit(1)

    success, failed = 0, []

    for i, etf_code in enumerate(targets):
        fund_code = CATHAY_ACTIVE_ETFS[etf_code]
        print(f"\n[{i+1}/{len(targets)}] {etf_code} (fundCode={fund_code})")

        holdings, tran_date = fetch_holdings(etf_code, fund_code)
        if holdings:
            if update_etf_json(etf_code, holdings, tran_date):
                success += 1
            else:
                failed.append(etf_code)
        else:
            failed.append(etf_code)

        if i < len(targets) - 1:
            time.sleep(1)

    print(f"\n國泰投信主動 ETF 更新完成 — 成功: {success}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
