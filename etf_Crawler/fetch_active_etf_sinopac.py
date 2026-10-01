#!/usr/bin/env python3
"""
永豐投信主動型 ETF 持股爬蟲

支援 ETF：
  00410A  主動永豐科技趨勢

資料來源：GET https://sitc.sinopac.com/SinopacEtfs/Etfs/Pcf/{code}
  伺服器端產生的申購買回清單 HTML，不需登入／token。（www.sitc.sinopac.com 解析不到，要用不帶 www 的網域。）
  指定日期要 POST 同一網址 fundId=&hDate=&op=1，hDate 是清單日不是持股日；GET 帶 ?hDate= 會被忽略。

日期：標題「（證劵代碼：00410A）YYYY/MM/DD」是申購買回清單日（下一交易日），
持股日取 <p class="cash_p">資料日期：YYYY/MM/DD</p>。2026-10-01 對過：清單 09-30 那份
（資料日期 09/29）與 CMoney 09-29 存檔 29 筆股數、權重完全相同。

更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

用法：
  uv run etf_Crawler/fetch_active_etf_sinopac.py           # 全部
  uv run etf_Crawler/fetch_active_etf_sinopac.py 00410A    # 指定
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

PAGE_URL = "https://sitc.sinopac.com/SinopacEtfs/Etfs/Pcf/{code}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
}

SINOPAC_ACTIVE_ETFS = ["00410A"]  # 主動永豐科技趨勢

DATA_DATE_RE = re.compile(r"資料日期：\s*(\d{4}/\d{2}/\d{2})")
PCF_DATE_RE = re.compile(r"證劵代碼：\w+）\s*(\d{4}/\d{2}/\d{2})")
# 第一個 tab_sh-w 是桌機版表格；後面的 tab_sh-m 是手機版，每檔一張表、內容重複
STOCK_TABLE_RE = re.compile(r'cash_title-s">股票</div>.*?<table class="tab_sh tab_sh-w[^>]*>(.*?)</table>', re.S)

session = create_session()


def _strip_tags(s: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def fetch_holdings(etf_code: str) -> tuple:
    """回傳 (holdings, tran_date_str)。"""
    url = PAGE_URL.format(code=etf_code)
    print(f"  抓取 {url}", end=" ... ", flush=True)

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=45)
            resp.raise_for_status()
            page = resp.text

            m = DATA_DATE_RE.search(page)
            if m:
                tran_date = m.group(1).replace("/", "-")
            else:
                p = PCF_DATE_RE.search(page)
                tran_date = pcf_to_holdings_date(p.group(1).replace("/", "-")) if p else None

            holdings = []
            t = STOCK_TABLE_RE.search(page)
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", t.group(1) if t else "", re.S):
                tds = [_strip_tags(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
                if len(tds) < 4:
                    continue
                code, name = tds[0], tds[1].replace(" ", "")
                try:
                    shares = int(float(tds[2].replace(",", "")))
                    weight = round(float(tds[-1].replace("%", "")), 2)
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
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(SINOPAC_ACTIVE_ETFS)

    success, failed, unchanged = 0, [], 0
    results: dict = {}

    for i, etf_code in enumerate(targets):
        if etf_code not in SINOPAC_ACTIVE_ETFS:
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
    print(f"\n永豐投信主動 ETF 更新完成 — 已更新: {success}，無變化: {unchanged}，失敗: {len(failed)}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
