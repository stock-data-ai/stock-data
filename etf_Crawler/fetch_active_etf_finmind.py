#!/usr/bin/env python3
"""
主動型 ETF 持股：FinMind 備援層（run_active_etf.py 第 3 層）

資料集：TaiwanStockActiveETFHolding（Sponsor 等級）。date 為持股日（2026-10-01 對過
00991A／00404A／00410A 等，與官網持股日股數相同）。

run_active_etf.py 只在 ETF_FINMIND_FALLBACK=true（repo Variable，2026-10-01 起開啟）時呼叫。
授權：2026-10-01 確認 FinMind Sponsor 條款允許對外呈現此資料集（見 stock_map 的資料來源與授權.md）。

輸出格式對齊 CMoney／官網舊檔：台股有 code、海外股只有名稱；期貨保留、選擇權不收；
現金列名稱 CASH、shares 放金額。

環境變數：FINMIND_API_TOKENS（逗號分隔取第一組）或 FINMIND_API_TOKEN。

用法：
  uv run etf_Crawler/fetch_active_etf_finmind.py 00981A 00996A
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from etf_utils import today_tw, write_github_output, write_holdings_update

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"
API = "https://api.finmindtrade.com/api/v4/data"
LOOKBACK_DAYS = 10


def _token() -> str:
    raw = os.environ.get("FINMIND_API_TOKENS") or os.environ.get("FINMIND_API_TOKEN") or ""
    return raw.split(",")[0].strip()


def fetch_holdings(etf_code: str, tk: str) -> tuple:
    """回傳 (holdings, tran_date)：近 LOOKBACK_DAYS 天內最新的一天。"""
    start = (date.fromisoformat(today_tw()) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    params = {"dataset": "TaiwanStockActiveETFHolding", "data_id": etf_code, "start_date": start}
    req = urllib.request.Request(f"{API}?{urllib.parse.urlencode(params)}",
                                 headers={"Authorization": f"Bearer {tk}"})
    print(f"  抓取 FinMind ({etf_code})", end=" ... ", flush=True)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = json.load(r)
            # FinMind 錯誤也是 HTTP 200，看 body.status（額度用盡、等級不足都走這條）
            if body.get("status") != 200:
                print(f"API 錯誤 status={body.get('status')} {body.get('msg')}")
                return [], None
            rows = body.get("data") or []
            break
        except Exception as e:                                 # noqa: BLE001
            print(f"失敗: {e}（第 {attempt + 1}/3 次）", end=" ... ")
            time.sleep(2 + attempt * 3)
    else:
        return [], None

    if not rows:
        print("無資料")
        return [], None
    tran_date = max(r["date"] for r in rows)
    holdings = []
    for r in rows:
        if r["date"] != tran_date:
            continue
        kind = r.get("asset_type")
        weight = round(float(r.get("weight") or 0), 2)
        if kind == "cash":
            if weight > 0:
                holdings.append({"name": "CASH", "weight": weight, "shares": int(r.get("market_value") or 0)})
            continue
        if kind not in ("stock", "futures"):
            continue
        shares = int(r.get("shares") or 0)
        if weight <= 0 and shares <= 0:
            continue
        entry = {"name": (r.get("component_stock_name") or "").strip(), "weight": weight,
                 "shares": shares if shares > 0 else None}
        code = (r.get("component_stock_id") or "").strip()
        if kind == "stock" and code.isdigit() and 4 <= len(code) <= 6:
            entry["code"] = code
        holdings.append(entry)
    holdings.sort(key=lambda x: -x["weight"])
    print(f"{len(holdings)} 筆，日期 {tran_date}")
    return holdings, tran_date


def main():
    targets = sys.argv[1:]
    if not targets:
        print("用法：fetch_active_etf_finmind.py <代號> ...")
        sys.exit(2)
    tk = _token()
    if not tk:
        print("[ERROR] 缺少 FINMIND_API_TOKENS / FINMIND_API_TOKEN")
        sys.exit(1)

    results, failed = {}, []
    for i, code in enumerate(targets):
        print(f"\n[{i + 1}/{len(targets)}] {code}")
        holdings, tran_date = fetch_holdings(code, tk)
        if holdings and write_holdings_update(ETF_DATA_DIR / f"{code}.json", code, holdings, tran_date):
            results[code] = ("updated", tran_date)
        else:
            failed.append(code)
            results[code] = ("failed", "")
    write_github_output(results)
    print(f"\nFinMind 備援完成 — 失敗: {len(failed)}/{len(targets)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
