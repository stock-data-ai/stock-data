# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
#   "beautifulsoup4",
# ]
# ///
"""
fetch_active_etf_fubon.py

從富邦投信 websys 系統爬取主動型 ETF 每日持股明細，
更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

原理：
  https://websys.fsit.com.tw/FubonETF/Fund/Assets.aspx?stkId={code}
  頁面為伺服器渲染 ASP.NET，持股資料直接嵌入 HTML 表格，無需額外 API。
  主站 www.fsit.com.tw 有地理封鎖，websys 子域名可正常存取。

支援 ETF：
  00405A  富邦台灣龍耀主動式ETF基金

用法：
    uv run etf_Crawler/fetch_active_etf_fubon.py          # 更新全部
    uv run etf_Crawler/fetch_active_etf_fubon.py 00405A   # 更新指定
"""

import re
import sys
import time
from etf_utils import create_session, write_github_output, write_holdings_update
from pathlib import Path
from typing import Optional

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("缺少依賴，請先執行：uv add requests beautifulsoup4")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

BASE_URL = "https://websys.fsit.com.tw/FubonETF/Fund/Assets.aspx?stkId={code}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

FUBON_ACTIVE_ETFS = [
    "00405A",  # 富邦台灣龍耀主動式ETF基金
]

session = create_session()


def fetch_holdings(etf_code: str) -> tuple:
    """
    回傳 (holdings, tran_date_str)
    holdings: [{"name": ..., "weight": ..., "code": ..., "shares": ...}, ...]
    tran_date_str: "2026-06-12" 或 None
    """
    url = BASE_URL.format(code=etf_code)
    print(f"  抓取 {url}")

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            txt = soup.get_text()

            m = re.search(r"資料日期[：:](\d{4}/\d{2}/\d{2})", txt)
            tran_date = m.group(1).replace("/", "-") if m else None

            holdings = []
            for table in soup.find_all("table"):
                rows = table.find_all("tr")
                if not rows:
                    continue
                headers = [th.get_text().strip() for th in rows[0].find_all(["td", "th"])]
                if "股票代碼" not in headers:
                    continue
                for row in rows[1:]:
                    cells = [td.get_text().strip().replace(",", "") for td in row.find_all("td")]
                    if len(cells) < 5:
                        continue
                    code, name, shares_raw, _, weight_raw = cells[0], cells[1], cells[2], cells[3], cells[4]
                    try:
                        weight = round(float(weight_raw), 2)
                        shares = int(float(shares_raw))
                    except (ValueError, TypeError):
                        continue
                    if weight <= 0:
                        continue
                    holdings.append({"name": name, "weight": weight, "shares": shares, "code": code})
                break

            if not holdings:
                print(f"  [WARN] 無持股資料（第 {attempt}/{max_attempts} 次）")
                if attempt < max_attempts:
                    time.sleep(attempt * 10)
                    continue
                return [], None

            return holdings, tran_date

        except Exception as e:
            print(f"  [ERROR] 抓取失敗: {e}（第 {attempt}/{max_attempts} 次）")
            if attempt < max_attempts:
                time.sleep(attempt * 10)

    return [], None


def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(FUBON_ACTIVE_ETFS)
    unknown = [t for t in targets if t not in FUBON_ACTIVE_ETFS]
    if unknown:
        print(f"[ERROR] 不支援的 ETF：{', '.join(unknown)}")
        print(f"支援清單：{', '.join(FUBON_ACTIVE_ETFS)}")
        sys.exit(1)

    success, failed, unchanged = 0, [], 0
    results: dict = {}

    for i, etf_code in enumerate(targets):
        print(f"\n[{i+1}/{len(targets)}] {etf_code}")
        holdings, tran_date = fetch_holdings(etf_code)
        if holdings:
            result = write_holdings_update(ETF_DATA_DIR / f"{etf_code}.json", etf_code, holdings, tran_date)
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
    print(f"\n富邦投信主動 ETF 更新完成 — 已更新: {success}，無變化: {unchanged}，失敗: {len(failed)}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
