# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
# ]
# ///
"""
fetch_active_etf_jpmorgan.py

從摩根資產管理 FundsMarketingHandler API 爬取主動型 ETF 每日持股明細。
不需要 Playwright，直接呼叫 API 即可取得完整持股（非前N筆）。

支援 ETF：
  00401A (CUSIP=TW00000401A1) 主動摩根台灣鑫收

用法：
    uv run etf_Crawler/fetch_active_etf_jpmorgan.py          # 更新全部
    uv run etf_Crawler/fetch_active_etf_jpmorgan.py 00401A   # 更新指定
"""

import json
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import requests
except ImportError:
    print("缺少依賴，請先執行：uv add requests")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

JPM_ACTIVE_ETFS = {
    "00401A": "TW00000401A1",  # 主動摩根台灣鑫收
}

API_URL = "https://am.jpmorgan.com/FundsMarketingHandler/product-data?cusip={cusip}&country=tw&role=twetf&lang=zh"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://am.jpmorgan.com/tw/zh/asset-management/twetf/",
}


def _clean_snapshot(h: dict) -> dict:
    clean: dict = {"name": h["name"], "weight": h["weight"]}
    if h.get("shares") is not None:
        clean["shares"] = h["shares"]
    if h.get("code"):
        clean["code"] = h["code"]
    return clean


def fetch_holdings(etf_code: str, cusip: str) -> Tuple[List[dict], Optional[str]]:
    url = API_URL.format(cusip=cusip)
    print(f"  抓取 {url}")

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()

        if not resp.content:
            print("  [ERROR] 回應為空")
            return [], None

        fd = resp.json().get("fundData", {})
        pcf = fd.get("holdings", {}).get("pcfEquityHoldings", {})
        raw = pcf.get("data") or []
        tran_date = pcf.get("effectiveDate")

        if not raw:
            print("  [SKIP] 無持股資料")
            return [], None

        holdings = []
        for item in raw:
            name = item.get("securityDescription", "").strip()
            weight = item.get("marketValuePercent")
            shares = item.get("shares")
            ticker = str(item.get("securityTicker") or "").strip()

            if not name or weight is None:
                continue

            try:
                weight = round(float(weight), 4)
            except (ValueError, TypeError):
                continue

            if weight <= 0:
                continue

            entry: dict = {"name": name, "weight": weight}
            if shares is not None:
                try:
                    entry["shares"] = int(shares)
                except (ValueError, TypeError):
                    pass
            # 台股代號（純數字 4 位）
            if ticker.isdigit() and 4 <= len(ticker) <= 6:
                entry["code"] = ticker

            holdings.append(entry)

        print(f"  找到 {len(holdings)} 筆持股，日期 {tran_date}")
        return holdings, tran_date

    except Exception as e:
        print(f"  [ERROR] 抓取失敗: {e}")
        return [], None


def update_etf_json(etf_code: str, holdings: list, tran_date: Optional[str],
                    history_only: bool = False) -> bool:
    json_path = ETF_DATA_DIR / f"{etf_code}.json"
    if not json_path.exists():
        print(f"  [WARN] 找不到 {json_path.name}")
        return False

    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        if not tran_date:
            print("  [SKIP] 無資料日期，跳過寫入")
            return False

        if "holdingsHistory" not in data:
            data["holdingsHistory"] = {}

        if history_only:
            if tran_date in data["holdingsHistory"]:
                print(f"  [SKIP] {tran_date} 已存在")
                return True
            data["holdingsHistory"][tran_date] = [_clean_snapshot(h) for h in holdings]
        else:
            prev_holdings = data.get("topHoldings", [])
            prev_date = data.get("lastUpdated")

            if tran_date == prev_date and prev_holdings:
                _key = lambda x: x.get("code") or x.get("name", "")
                if (sorted([_clean_snapshot(h) for h in holdings], key=_key) ==
                        sorted([_clean_snapshot(h) for h in prev_holdings], key=_key)):
                    print(f"  [SKIP] {etf_code} 數據無變化（{tran_date}），跳過寫入")
                    return True

            if prev_date and prev_holdings and prev_date not in data["holdingsHistory"]:
                data["holdingsHistory"][prev_date] = [_clean_snapshot(h) for h in prev_holdings]

            prev_map = {h.get("code") or h.get("name"): h for h in prev_holdings}
            for h in holdings:
                key = h.get("code") or h.get("name")
                prev = prev_map.get(key)
                if prev:
                    h["previousWeight"] = prev.get("weight", 0)
                    h["weightChange"] = round(h["weight"] - h["previousWeight"], 4)
                    prev_s = prev.get("shares") or 0
                    h["previousShares"] = prev_s
                    h["sharesChange"] = (h["shares"] - prev_s) if h.get("shares") is not None and prev_s else 0
                else:
                    h["previousWeight"] = 0
                    h["weightChange"] = h["weight"]
                    h["previousShares"] = 0
                    h["sharesChange"] = h.get("shares") or 0

            data["topHoldings"] = holdings
            data["holdingsHistory"][tran_date] = [_clean_snapshot(h) for h in holdings]
            data["lastUpdated"] = tran_date

        sorted_dates = sorted(data["holdingsHistory"].keys(), reverse=True)
        for old_d in sorted_dates[30:]:
            del data["holdingsHistory"][old_d]

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"  [OK] {etf_code} — {len(holdings)} 筆持股，{tran_date}")
        return True

    except Exception as e:
        print(f"  [ERROR] 寫入失敗: {e}")
        return False


def _parse_args():
    args = sys.argv[1:]
    etf_codes = [a for a in args if not a.startswith("--")]
    targets = etf_codes if etf_codes else list(JPM_ACTIVE_ETFS.keys())
    unknown = [t for t in targets if t not in JPM_ACTIVE_ETFS]
    if unknown:
        print(f"[ERROR] 不支援的 ETF：{', '.join(unknown)}")
        print(f"支援清單：{', '.join(JPM_ACTIVE_ETFS)}")
        sys.exit(1)
    return targets


def main():
    targets = _parse_args()
    success, failed = 0, []

    for i, etf_code in enumerate(targets):
        cusip = JPM_ACTIVE_ETFS[etf_code]
        print(f"\n[{i+1}/{len(targets)}] {etf_code}")
        holdings, tran_date = fetch_holdings(etf_code, cusip)
        if holdings:
            if update_etf_json(etf_code, holdings, tran_date):
                success += 1
            else:
                failed.append(etf_code)
        else:
            failed.append(etf_code)

        if i < len(targets) - 1:
            time.sleep(1)

    print(f"\n摩根投信主動 ETF 更新完成 — 成功: {success}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")


if __name__ == "__main__":
    main()
