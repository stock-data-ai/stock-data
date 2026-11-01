#!/usr/bin/env python3
"""
聯博投信主動型 ETF 持股爬蟲

支援 ETF：
  00404A  主動聯博動能50 (shareClassId: TW00000404A5)

資料來源：GET https://webapi.alliancebernstein.com/v2/funds/tw/zh-tw/investor/{shareClassId}/holdings
  公開 JSON，不需 token／cookie。可帶 ?date=YYYY-MM-DD（持股日），不帶取最新。
  官網申購買回清單頁（abfunds.com.tw/zh-tw/etfs/pcf.TW00000404A5.html）是 React 殼，資料來自這支。
  休市日或未來日期回 200、domesticHoldings 為 null。

日期：domesticHoldings[].asOfDate（MM/DD/YYYY）就是持股日。另一支 /basket 的 date 參數是
清單公告日，不要拿來當持股日。2026-10-01 對過：?date=2026-09-29 與 CMoney 09-29 存檔
52 檔股數完全相同、期貨列一致。

輸出對齊 CMoney 舊檔：股票＋期貨（期貨無代號），選擇權不收（CMoney 也沒有）。
名稱沿用官網全名（「台灣積體電路製造」），CMoney 存的也是這個。

用法：
  uv run etf_Crawler/fetch_active_etf_allianceb.py           # 全部
  uv run etf_Crawler/fetch_active_etf_allianceb.py 00404A    # 指定
"""

import sys
import time
from pathlib import Path
from typing import Optional

from etf_utils import create_session, write_github_output, write_holdings_update

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

API_URL = "https://webapi.alliancebernstein.com/v2/funds/tw/zh-tw/investor/{share_class}/holdings"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

AB_ACTIVE_ETFS = {
    "00404A": "TW00000404A5",  # 主動聯博動能50
}

KEEP_CATEGORIES = ("holdings-section-equity", "holdings-section-futures")

session = create_session()


def fetch_holdings(etf_code: str) -> tuple:
    """回傳 (holdings, tran_date_str)。"""
    url = API_URL.format(share_class=AB_ACTIVE_ETFS[etf_code])
    print(f"  抓取 {etf_code} ({AB_ACTIVE_ETFS[etf_code]})", end=" ... ", flush=True)

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=45)
            resp.raise_for_status()
            sections = resp.json().get("domesticHoldings") or []

            tran_date = None
            holdings = []
            for sec in sections:
                if not tran_date and sec.get("asOfDate"):
                    mm, dd, yyyy = sec["asOfDate"].split("/")
                    tran_date = f"{yyyy}-{mm}-{dd}"
                if sec.get("holdingCategory") not in KEEP_CATEGORIES:
                    continue
                for h in sec.get("holdings") or []:
                    try:
                        weight = round(float(h.get("holdingPerc") or 0), 2)
                        shares = int(float(h.get("holdingShares") or 0))
                    except (TypeError, ValueError):
                        continue
                    if weight <= 0 and shares <= 0:
                        continue
                    entry = {"name": (h.get("holding") or "").strip(), "weight": weight,
                             "shares": shares if shares > 0 else None}
                    code = (h.get("holdingCode") or "").strip()
                    if code.isdigit() and 4 <= len(code) <= 6:
                        entry["code"] = code
                    holdings.append(entry)
            holdings.sort(key=lambda x: -x["weight"])   # API 回傳沒有排序

            if not holdings:
                print(f"無持股資料（第 {attempt}/{max_attempts} 次）")
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
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(AB_ACTIVE_ETFS.keys())

    success, failed, unchanged = 0, [], 0
    results: dict = {}

    for i, etf_code in enumerate(targets):
        if etf_code not in AB_ACTIVE_ETFS:
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
    print(f"\n聯博投信主動 ETF 更新完成 — 已更新: {success}，無變化: {unchanged}，失敗: {len(failed)}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
