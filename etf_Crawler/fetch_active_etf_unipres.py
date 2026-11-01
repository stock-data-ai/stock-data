# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
#   "beautifulsoup4",
# ]
# ///
"""
fetch_active_etf_unipres.py

從統一投信官網 (ezmoney.com.tw) 爬取主動型 ETF 每日持股明細，
更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

支援 ETF：
  00981A (fundCode=49YTW) 主動統一台股增長
  00988A (fundCode=61YTW) 主動統一全球創新
  00403A (fundCode=63YTW) 主動統一升級50

用法：
    uv run etf_Crawler/fetch_active_etf_unipres.py          # 更新全部
    uv run etf_Crawler/fetch_active_etf_unipres.py 00981A   # 更新指定
"""

import json
import sys
import html as html_module
import time
from datetime import date
from etf_utils import create_session, write_github_output, write_holdings_update
from pathlib import Path
from typing import Optional

try:
    import requests
    import urllib3
    from bs4 import BeautifulSoup
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    print("缺少依賴，請先執行：uv add requests beautifulsoup4")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

BASE_URL = "https://www.ezmoney.com.tw/ETF/Fund/Info"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}

# 統一投信主動型 ETF 對照表：ETF代號 → ezmoney fundCode
UNIPRES_ACTIVE_ETFS = {
    "00981A": "49YTW",
    "00988A": "61YTW",
    "00403A": "63YTW",
}

def fetch_holdings(session: requests.Session, etf_code: str, fund_code: str) -> tuple:
    """
    回傳 (holdings, tran_date_str)
    holdings: [{"name": ..., "weight": ..., "code": ...}, ...]
    tran_date_str: "2026-04-08" 或 None
    """
    url = f"{BASE_URL}?fundCode={fund_code}&tabName=asset"
    print(f"  抓取 {url}")

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=45, verify=False)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            elem = soup.find(id="DataAsset")
            if not elem:
                print(f"  [WARN] 找不到 DataAsset 元素（第 {attempt}/{max_attempts} 次）")
                print(f"  [DEBUG] HTTP {resp.status_code}, Content-Type: {resp.headers.get('Content-Type', '')}")
                print(f"  [DEBUG] Response (前500字):\n{resp.text[:500]}")
                if attempt < max_attempts:
                    wait = attempt * 10
                    print(f"  [RETRY] {wait} 秒後重試...")
                    time.sleep(wait)
                    session.get(BASE_URL, headers=HEADERS, timeout=45, verify=False)
                    continue
                return [], None

            raw = html_module.unescape(elem.get("data-content", ""))
            asset_data: list[dict] = json.loads(raw)

            # 找股票資產區塊
            stock_block = next((a for a in asset_data if a.get("AssetCode") == "ST"), None)
            if not stock_block or not stock_block.get("Details"):
                print(f"  [WARN] 無股票持股資料")
                return [], None

            details = stock_block["Details"]
            tran_date = details[0].get("TranDate", "")[:10] if details else None

            holdings = []
            for d in details:
                code_raw = str(d.get("DetailCode", "")).strip()
                name = str(d.get("DetailName", "")).strip()
                weight = d.get("NavRate")
                shares = d.get("Share")

                if weight is None:
                    continue
                try:
                    weight = round(float(weight), 2)
                except (ValueError, TypeError):
                    continue
                if weight <= 0:
                    continue

                entry: dict = {
                    "name": name,
                    "weight": weight,
                    "shares": int(shares) if shares is not None else None
                }
                # 台股代號：4-6位純數字
                if code_raw.isdigit() and 4 <= len(code_raw) <= 6:
                    entry["code"] = code_raw
                elif code_raw:
                    entry["foreignCode"] = code_raw

                holdings.append(entry)

            return holdings, tran_date

        except Exception as e:
            print(f"  [ERROR] 抓取失敗: {e}（第 {attempt}/{max_attempts} 次）")
            if attempt < max_attempts:
                wait = attempt * 10
                print(f"  [RETRY] {wait} 秒後重試...")
                time.sleep(wait)
                session.get(BASE_URL, headers=HEADERS, timeout=45, verify=False)

    return [], None


def update_etf_json(etf_code: str, holdings: list, tran_date: Optional[str]) -> bool:
    return write_holdings_update(
        ETF_DATA_DIR / f"{etf_code}.json",
        etf_code, holdings, tran_date,
        has_foreign_code=True,
    )



def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(UNIPRES_ACTIVE_ETFS.keys())
    unknown = [t for t in targets if t not in UNIPRES_ACTIVE_ETFS]
    if unknown:
        print(f"[ERROR] 不支援的 ETF：{', '.join(unknown)}")
        print(f"支援清單：{', '.join(UNIPRES_ACTIVE_ETFS.keys())}")
        sys.exit(1)

    session = create_session()
    success, failed, unchanged = 0, [], 0
    results: dict = {}

    for i, etf_code in enumerate(targets):
        fund_code = UNIPRES_ACTIVE_ETFS[etf_code]
        print(f"\n[{i+1}/{len(targets)}] {etf_code} (fundCode={fund_code})")

        holdings, tran_date = fetch_holdings(session, etf_code, fund_code)
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
    print(f"\n統一投信主動 ETF 更新完成 — 已更新: {success}，無變化: {unchanged}，失敗: {len(failed)}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
