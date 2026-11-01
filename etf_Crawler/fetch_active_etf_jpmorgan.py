#!/usr/bin/env python3
"""
摩根投信主動型 ETF 持股爬蟲

支援 ETF：
  00401A  主動摩根台灣鑫收 (ISIN: TW00000401A1)
  00989A  主動摩根美國科技 (ISIN: TW00000989A5)

資料來源：GET https://am.jpmorgan.com/FundsMarketingHandler/product-data
  ?cusip={ISIN}&country=tw&role=twetf&language=zh&userLoggedIn=false
  官網 twetf 產品頁背後的 JSON，不需 cookie／token。持股在 fundData.holdings.pcf*Holdings。

日期：pcfEquityHoldings.effectiveDate 是持股日；breakdowns.m12Details.effectiveDate 是
申購買回清單日（下一交易日），不要用。00989A（美股）持股日本來就落後一天。
2026-10-01 對過：持股日 09-29 的那份與 CMoney 09-29 存檔股數全部相同（00401A 56/56、00989A 68/68）。

輸出對齊 CMoney 舊檔，換來源後加減碼比較才接得上：
  - 台股有 code；美股不帶代號（CMoney 舊檔就只有英文名），以名稱比對
  - 現金每個幣別一列，名稱 CASH、shares 放金額（同 CMoney）
  - 期貨保留、選擇權不收（同 CMoney）

用法：
  uv run etf_Crawler/fetch_active_etf_jpmorgan.py           # 全部
  uv run etf_Crawler/fetch_active_etf_jpmorgan.py 00401A    # 指定
"""

import sys
import time
from pathlib import Path
from typing import Optional

from etf_utils import create_session, write_github_output, write_holdings_update

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

API_URL = "https://am.jpmorgan.com/FundsMarketingHandler/product-data"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

JPM_ACTIVE_ETFS = {
    "00401A": "TW00000401A1",  # 主動摩根台灣鑫收
    "00989A": "TW00000989A5",  # 主動摩根美國科技
}

session = create_session()


def _rows(section: dict) -> list:
    return (section or {}).get("data") or []


def fetch_holdings(etf_code: str) -> tuple:
    """回傳 (holdings, tran_date_str)。"""
    isin = JPM_ACTIVE_ETFS[etf_code]
    params = {"cusip": isin, "country": "tw", "role": "twetf", "language": "zh", "userLoggedIn": "false"}
    print(f"  抓取 {etf_code} ({isin})", end=" ... ", flush=True)

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(API_URL, params=params, headers=HEADERS, timeout=45)
            resp.raise_for_status()
            h = (resp.json().get("fundData") or {}).get("holdings") or {}
            equity = h.get("pcfEquityHoldings") or {}
            tran_date = equity.get("effectiveDate")

            holdings = []
            for r in _rows(equity) + _rows(h.get("pcfFutureHoldings")):
                try:
                    weight = round(float(r.get("marketValuePercent") or 0), 2)
                    shares = int(float(r.get("shares") or 0))
                except (TypeError, ValueError):
                    continue
                if weight <= 0 and shares <= 0:
                    continue
                entry = {"name": (r.get("securityDescription") or "").strip(), "weight": weight,
                         "shares": shares if shares > 0 else None}
                code = (r.get("securityTicker") or "").strip()
                if code.isdigit() and 4 <= len(code) <= 6:
                    entry["code"] = code
                holdings.append(entry)
            for r in _rows(h.get("pcfCashHoldings")):
                try:
                    weight = round(float(r.get("marketValuePercent") or 0), 2)
                    amount = int(float(r.get("marketValueBase") or 0))
                except (TypeError, ValueError):
                    continue
                if weight > 0:
                    holdings.append({"name": "CASH", "weight": weight, "shares": amount})
            holdings.sort(key=lambda x: -x["weight"])

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
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(JPM_ACTIVE_ETFS.keys())

    success, failed, unchanged = 0, [], 0
    results: dict = {}

    for i, etf_code in enumerate(targets):
        if etf_code not in JPM_ACTIVE_ETFS:
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
    print(f"\n摩根投信主動 ETF 更新完成 — 已更新: {success}，無變化: {unchanged}，失敗: {len(failed)}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
