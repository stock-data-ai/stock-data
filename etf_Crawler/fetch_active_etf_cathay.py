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
from pathlib import Path
from typing import Optional

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
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


def create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.5,
        status_forcelist=[408, 429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


session = create_session()


def _clean_name(name: str) -> str:
    """去除股票名稱中的多餘空白。"""
    return " ".join(name.split())


def _clean_snapshot(h: dict) -> dict:
    clean: dict = {"name": h["name"], "weight": h["weight"]}
    if h.get("code"):
        clean["code"] = h["code"]
    return clean


def fetch_holdings(etf_code: str, fund_code: str) -> tuple:
    """
    回傳 (holdings, date_str)
    holdings: [{"name": ..., "weight": ..., "code": ...}, ...]
    date_str: "2026-05-12" 或 None
    """
    url = f"{API_BASE}?fundCode={fund_code}"
    print(f"  抓取 {url}")

    try:
        resp = session.get(url, headers=BASE_HEADERS, timeout=45)
        resp.raise_for_status()
        data = resp.json()

        if not data.get("success"):
            print(f"  [WARN] API 回應失敗: {data.get('returnMessage')}")
            return [], None

        result = data.get("result", {})
        stocks = result.get("stockWeights", [])
        if not stocks:
            print(f"  [WARN] 無持股資料")
            return [], None

        # 日期格式 "2026/05/12" → "2026-05-12"
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
        print(f"  [ERROR] 抓取失敗: {e}")
        return [], None


def update_etf_json(etf_code: str, holdings: list, tran_date: Optional[str]) -> bool:
    json_path = ETF_DATA_DIR / f"{etf_code}.json"
    if not json_path.exists():
        print(f"  [WARN] 找不到 {json_path.name}")
        return False

    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        if not tran_date:
            print(f"  [SKIP] 無法取得資料日期，跳過寫入")
            return False

        prev_holdings = data.get("topHoldings", [])
        prev_date = data.get("lastUpdated")

        if tran_date == prev_date and prev_holdings:
            _key = lambda x: x.get("code") or x.get("name", "")
            if sorted([_clean_snapshot(h) for h in holdings], key=_key) == \
               sorted([_clean_snapshot(h) for h in prev_holdings], key=_key):
                print(f"  [SKIP] {etf_code} 數據無變化（{tran_date}），跳過寫入")
                return True

        if "holdingsHistory" not in data:
            data["holdingsHistory"] = {}

        if prev_date and prev_holdings and prev_date not in data["holdingsHistory"]:
            data["holdingsHistory"][prev_date] = [_clean_snapshot(h) for h in prev_holdings]

        prev_map = {h.get("code") or h.get("name"): h for h in prev_holdings}
        for h in holdings:
            key = h.get("code") or h.get("name")
            prev = prev_map.get(key)
            if prev:
                prev_w = prev.get("weight", 0)
                h["previousWeight"] = prev_w
                h["weightChange"] = round(h["weight"] - prev_w, 2)
            else:
                h["previousWeight"] = 0
                h["weightChange"] = h["weight"]

        data["topHoldings"] = holdings
        data["holdingsHistory"][tran_date] = [_clean_snapshot(h) for h in holdings]

        sorted_dates = sorted(data["holdingsHistory"].keys(), reverse=True)
        for old_d in sorted_dates[30:]:
            del data["holdingsHistory"][old_d]

        data["lastUpdated"] = tran_date

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"  [OK] {etf_code} — {len(holdings)} 筆持股，資料日期 {tran_date}")
        return True

    except Exception as e:
        print(f"  [ERROR] 寫入失敗: {e}")
        import traceback
        traceback.print_exc()
        return False


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