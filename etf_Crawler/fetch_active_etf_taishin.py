# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
# ]
# ///
"""
fetch_active_etf_taishin.py

從台新投信官網爬取主動型 ETF 每日持股明細，
更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

原理：
  持股資料直接內嵌於 HTML 頁面（無需另呼叫 API），
  解析 /ETF/Home/ETFSeriesDetail/{code} 的持股表格。

支援 ETF：
  00986A  台新全球龍頭成長主動式ETF基金
  00987A  台新台灣優勢成長主動式ETF基金

用法：
    uv run etf_Crawler/fetch_active_etf_taishin.py          # 更新全部
    uv run etf_Crawler/fetch_active_etf_taishin.py 00987A   # 更新指定
    uv run etf_Crawler/fetch_active_etf_taishin.py 00986A 00987A  # 更新多檔
"""

import json
import re
import sys
import time
from datetime import date
from etf_utils import create_session, write_github_output, write_holdings_update
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("缺少依賴，請先執行：uv add requests")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

BASE_URL = "https://www.tsit.com.tw/ETF/Home/ETFSeriesDetail"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

TAISHIN_ACTIVE_ETFS = [
    "00986A",  # 台新全球龍頭成長主動式ETF基金
    "00987A",  # 台新台灣優勢成長主動式ETF基金
]

session = create_session()


def fetch_holdings(etf_code: str) -> tuple:
    """
    爬取持股頁面，回傳 (holdings, data_date)。
    holdings: [{"name": ..., "weight": ..., "code": ..., "shares": ...}, ...]
    data_date: "2026-04-30" 或 None
    """
    url = f"{BASE_URL}/{etf_code}"
    print(f"  抓取 {url}", end=" ... ", flush=True)

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=45)
            resp.raise_for_status()
            html = resp.text

            m = re.search(r'id="PUB_DATE"[^>]+value="([^"]+)"', html)
            data_date = m.group(1) if m else None

            idx = html.find('<th>代號</th>')
            if idx == -1:
                print(f"找不到持股表格（第 {attempt}/{max_attempts} 次）")
                if attempt < max_attempts:
                    time.sleep(attempt * 10)
                    continue
                return [], data_date

            section = html[idx:idx + 30000]

            rows = re.findall(
                r'<tr>\s*<td>([^<]+)</td>\s*<td>([^<]+)</td>\s*<td>([^<]+)</td>\s*<td>([^<]+)</td>\s*</tr>',
                section,
            )

            holdings = []
            for code_raw, name, shares_raw, weight_raw in rows:
                code_raw = code_raw.strip()
                name = name.strip()
                shares_raw = shares_raw.strip()
                weight_raw = weight_raw.strip()

                try:
                    weight = round(float(weight_raw.rstrip("%").replace(",", "")), 2)
                except (ValueError, TypeError):
                    continue
                if weight <= 0:
                    continue

                try:
                    shares = int(shares_raw.replace(",", ""))
                except (ValueError, TypeError):
                    shares = None

                entry: dict = {"name": name, "weight": weight, "shares": shares}

                parts = code_raw.split()
                numeric = parts[0] if parts else ""
                if numeric.isdigit() and 4 <= len(numeric) <= 6:
                    entry["code"] = numeric
                elif code_raw:
                    entry["foreignCode"] = code_raw

                holdings.append(entry)

            print(f"{len(holdings)} 筆，日期 {data_date}")
            return holdings, data_date

        except Exception as e:
            print(f"失敗: {e}（第 {attempt}/{max_attempts} 次）")
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
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(TAISHIN_ACTIVE_ETFS)
    unknown = [t for t in targets if t not in TAISHIN_ACTIVE_ETFS]
    if unknown:
        print(f"[ERROR] 不支援的 ETF：{', '.join(unknown)}")
        print(f"支援清單：{', '.join(TAISHIN_ACTIVE_ETFS)}")
        sys.exit(1)

    success, failed, unchanged = 0, [], 0
    results: dict = {}

    for i, etf_code in enumerate(targets):
        print(f"\n[{i+1}/{len(targets)}] {etf_code}")
        holdings, data_date = fetch_holdings(etf_code)
        if holdings:
            result = update_etf_json(etf_code, holdings, data_date)
            if result is True:
                success += 1
                results[etf_code] = ("updated", data_date or "")
            elif result == "unchanged":
                unchanged += 1
                results[etf_code] = ("unchanged", data_date or "")
            else:
                failed.append(etf_code)
                results[etf_code] = ("failed", "")
        else:
            failed.append(etf_code)
            results[etf_code] = ("failed", "")

        if i < len(targets) - 1:
            time.sleep(1)

    write_github_output(results)
    print(f"\n台新投信主動 ETF 更新完成 — 已更新: {success}，無變化: {unchanged}，失敗: {len(failed)}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
