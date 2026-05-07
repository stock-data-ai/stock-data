# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
# ]
# ///
"""
fetch_active_etf_capital.py

從群益投信官網 API 爬取主動型 ETF 每日持股明細，
更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

支援 ETF：
  00982A (fundId=399) 群益台灣精選強棒主動式ETF基金
  00992A (fundId=500) 群益台灣科技創新主動式ETF基金
  00997A (fundId=502) 群益美國增長主動式ETF基金

API：POST https://www.capitalfund.com.tw/CFWeb/api/etf/buyback
     Body: {"fundId": "<id>", "date": null}

用法：
    uv run etf_Crawler/fetch_active_etf_capital.py                   # 更新全部
    uv run etf_Crawler/fetch_active_etf_capital.py 00982A            # 更新指定
    uv run etf_Crawler/fetch_active_etf_capital.py 00982A 00992A     # 更新多檔
"""

import json
import sys
import time
from datetime import date
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

API_URL = "https://www.capitalfund.com.tw/CFWeb/api/etf/buyback"
BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}

# 群益投信主動型 ETF 對照表：ETF代號 → fundId
CAPITAL_ACTIVE_ETFS = {
    "00982A": "399",   # 群益台灣精選強棒主動式ETF基金
    "00992A": "500",   # 群益台灣科技創新主動式ETF基金
    "00997A": "502",   # 群益美國增長主動式ETF基金
}


def create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.5,
        status_forcelist=[408, 429, 500, 502, 503, 504],
        allowed_methods=["POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


session = create_session()


def _parse_date(date1: str) -> Optional[str]:
    """將 '2026/4/10 上午 12:00:00' 解析為 '2026-04-10'。"""
    try:
        parts = date1.split(" ")[0].split("/")
        y, m, d = parts[0], parts[1].zfill(2), parts[2].zfill(2)
        return f"{y}-{m}-{d}"
    except Exception:
        return None


def _clean_snapshot(h: dict) -> dict:
    """只保留歷史快照應有的欄位，去除比較欄位。"""
    clean: dict = {"name": h["name"], "weight": h["weight"]}
    if h.get("shares") is not None:
        clean["shares"] = h["shares"]
    if h.get("code"):
        clean["code"] = h["code"]
    return clean


def fetch_holdings(etf_code: str, fund_id: str) -> tuple:
    """
    回傳 (holdings, tran_date_str)
    holdings: [{"name": ..., "weight": ..., "code": ..., "shares": ...}, ...]
    tran_date_str: "2026-04-10" 或 None
    """
    print(f"  抓取 {API_URL} (fundId={fund_id})")

    try:
        headers = {
            **BASE_HEADERS,
            "Referer": f"https://www.capitalfund.com.tw/etf/product/detail/{fund_id}/portfolio",
        }
        resp = session.post(
            API_URL,
            headers=headers,
            json={"fundId": fund_id, "date": None},
            timeout=45,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 200:
            print(f"  [WARN] API 回應 code={data.get('code')}")
            return [], None

        stocks = data.get("data", {}).get("stocks", [])
        if not stocks:
            print(f"  [WARN] 無持股資料")
            return [], None

        # 解析日期（取第一筆的 date1）
        tran_date = _parse_date(stocks[0].get("date1", ""))


        holdings = []
        for s in stocks:
            code_raw = str(s.get("stocNo", "")).strip()
            name = str(s.get("stocName", "")).strip()
            weight = s.get("weight")
            shares = s.get("share")

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
                "shares": int(shares) if shares is not None else None,
            }
            # 台股代號：4-6位純數字
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
        current_date = tran_date

        if current_date == prev_date and prev_holdings:
            _key = lambda x: x.get("code") or x.get("name", "")
            if sorted([_clean_snapshot(h) for h in holdings], key=_key) == sorted([_clean_snapshot(h) for h in prev_holdings], key=_key):
                print(f"  [SKIP] {etf_code} 數據無變化（{current_date}），跳過寫入")
                return True

        # 初始化歷史紀錄
        if "holdingsHistory" not in data:
            data["holdingsHistory"] = {}

        # 如果有舊資料且尚未存入歷史，先補存
        if prev_date and prev_holdings and prev_date not in data["holdingsHistory"]:
            data["holdingsHistory"][prev_date] = [_clean_snapshot(h) for h in prev_holdings]

        # 計算 topHoldings 的比較欄位
        prev_map = {h.get("code") or h.get("name"): h for h in prev_holdings}
        for h in holdings:
            key = h.get("code") or h.get("name")
            prev = prev_map.get(key)
            if prev:
                prev_w = prev.get("weight", 0)
                h["previousWeight"] = prev_w
                h["weightChange"] = round(h["weight"] - prev_w, 2)
                prev_s = prev.get("shares") or 0
                h["previousShares"] = prev_s
                h["sharesChange"] = (h["shares"] - prev_s) if h["shares"] is not None and prev_s else 0
            else:
                h["previousWeight"] = 0
                h["weightChange"] = h["weight"]
                h["previousShares"] = 0
                h["sharesChange"] = h["shares"] or 0

        data["topHoldings"] = holdings

        # 歷史快照只存乾淨欄位
        data["holdingsHistory"][current_date] = [_clean_snapshot(h) for h in holdings]

        # 限制 30 天
        sorted_dates = sorted(data["holdingsHistory"].keys(), reverse=True)
        for old_d in sorted_dates[30:]:
            del data["holdingsHistory"][old_d]

        data["lastUpdated"] = current_date

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"  [OK] {etf_code} — {len(holdings)} 筆持股，資料日期 {current_date}")
        return True
    except Exception as e:
        print(f"  [ERROR] 寫入失敗: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(CAPITAL_ACTIVE_ETFS.keys())
    unknown = [t for t in targets if t not in CAPITAL_ACTIVE_ETFS]
    if unknown:
        print(f"[ERROR] 不支援的 ETF：{', '.join(unknown)}")
        print(f"支援清單：{', '.join(CAPITAL_ACTIVE_ETFS.keys())}")
        sys.exit(1)

    success, failed = 0, []

    for i, etf_code in enumerate(targets):
        fund_id = CAPITAL_ACTIVE_ETFS[etf_code]
        print(f"\n[{i+1}/{len(targets)}] {etf_code} (fundId={fund_id})")

        holdings, tran_date = fetch_holdings(etf_code, fund_id)
        if holdings:
            if update_etf_json(etf_code, holdings, tran_date):
                success += 1
            else:
                failed.append(etf_code)
        else:
            failed.append(etf_code)

        if i < len(targets) - 1:
            time.sleep(1)

    print(f"\n群益投信主動 ETF 更新完成 — 成功: {success}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
