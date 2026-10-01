#!/usr/bin/env python3
"""
凱基投信主動型 ETF 持股爬蟲

支援 ETF：
  00407A  主動凱基台灣 (fundID: J024)

資料來源：POST https://www.kgifund.com.tw/Fund/RedemptionVC
  Body（form）：fundID=J024[&queryDate=YYYY/MM/DD]；不帶 queryDate 取最新一份。
  回傳現金申購買回清單的 HTML 片段（/Fund/RedemptionList 頁面自己 .load() 的那支）。

日期：頁面的 DataDate／標題是「申購買回清單公告日」（下一交易日），不是持股日。
持股日取「(YYYY/MM/DD)每受益權單位淨資產價值」括號裡的淨值日；2026-10-01 對過三組：
公告 10/02 ↔ 淨值 10/01、10/01 ↔ 09/30、09/30 ↔ 09/29，且淨值 09/30 那份的股數與權重
和 CMoney 09-30 存檔 50 筆完全相同。抓不到淨值日才退回 pcf_to_holdings_date(DataDate)。

更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

用法：
  uv run etf_Crawler/fetch_active_etf_kgi.py           # 全部
  uv run etf_Crawler/fetch_active_etf_kgi.py 00407A    # 指定
"""

import html as html_lib
import re
import sys
import time
from pathlib import Path
from typing import Optional

from etf_utils import create_session, pcf_to_holdings_date, write_github_output, write_holdings_update

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

API_URL = "https://www.kgifund.com.tw/Fund/RedemptionVC"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.kgifund.com.tw/Fund/RedemptionList",
}

# 內部 ID 取自 /Fund/RedemptionList 的下拉選單
KGI_ACTIVE_ETFS = {
    "00407A": "J024",  # 主動凱基台灣
}

ROW_RE = re.compile(
    r'<tr name="content"[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>'
    r'\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>',
    re.S,
)
NAV_DATE_RE = re.compile(r"\((\d{4}/\d{2}/\d{2})\)每受益權單位淨資產價值")
PUB_DATE_RE = re.compile(r'id="DataDate"[^>]*value="(\d{4}/\d{2}/\d{2})"')

session = create_session()


def _strip_tags(s: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def fetch_holdings(etf_code: str) -> tuple:
    """回傳 (holdings, tran_date_str)。"""
    fund_id = KGI_ACTIVE_ETFS[etf_code]
    print(f"  抓取 {etf_code} (fundID={fund_id})", end=" ... ", flush=True)

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.post(API_URL, data={"fundID": fund_id}, headers=HEADERS, timeout=45)
            resp.raise_for_status()
            page = resp.text

            m = NAV_DATE_RE.search(page)
            if m:
                tran_date = m.group(1).replace("/", "-")
            else:
                p = PUB_DATE_RE.search(page)
                tran_date = pcf_to_holdings_date(p.group(1).replace("/", "-")) if p else None

            holdings = []
            for code, name, shares_s, weight_s in ROW_RE.findall(page):
                code = _strip_tags(code)
                name = _strip_tags(name).rstrip("*").replace(" ", "")
                try:
                    shares = int(float(_strip_tags(shares_s).replace(",", "")))
                    weight = round(float(_strip_tags(weight_s).replace("%", "")), 2)
                except ValueError:
                    continue
                if weight <= 0 and shares <= 0:
                    continue
                entry = {"name": name, "weight": weight, "shares": shares if shares > 0 else None}
                if code.isdigit() and 4 <= len(code) <= 6:
                    entry["code"] = code
                holdings.append(entry)

            if not holdings:
                print(f"找不到持股表格（第 {attempt}/{max_attempts} 次）")
                if attempt < max_attempts:
                    time.sleep(attempt * 10)
                    continue
                return [], tran_date

            print(f"{len(holdings)} 筆，日期 {tran_date}")
            return holdings, tran_date

        except Exception as e:
            print(f"失敗: {e}（第 {attempt}/{max_attempts} 次）")
            if attempt < max_attempts:
                time.sleep(attempt * 10)

    return [], None


def update_etf_json(etf_code: str, holdings: list, tran_date: Optional[str]) -> bool:
    return write_holdings_update(ETF_DATA_DIR / f"{etf_code}.json", etf_code, holdings, tran_date)


def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(KGI_ACTIVE_ETFS.keys())

    success, failed, unchanged = 0, [], 0
    results: dict = {}

    for i, etf_code in enumerate(targets):
        if etf_code not in KGI_ACTIVE_ETFS:
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
    print(f"\n凱基投信主動 ETF 更新完成 — 已更新: {success}，無變化: {unchanged}，失敗: {len(failed)}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
