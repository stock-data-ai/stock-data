# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
# ]
# ///
"""
fetch_active_etf_first_financial.py

從第一金投信官網 (fsitc.com.tw) 爬取主動型 ETF 每日持股明細，
更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

支援 ETF：
  00994A  主動第一金台股優 (Internal ID: 182)
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

try:
    import requests
except ImportError:
    print("缺少依賴，請先執行：uv add requests")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

API_URL = "https://www.fsitc.com.tw/WebAPI.aspx/Get_hd"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json; charset=utf-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

# 第一金投信主動型 ETF 代號與內部 ID 映射
FIRST_FINANCIAL_ACTIVE_ETFS = {
    "00994A": "182",  # 主動第一金台股優
}


def fetch_holdings(etf_code: str) -> tuple:
    """
    回傳 (holdings, tran_date_str)
    holdings: [{"name": ..., "weight": ..., "code": ..., "shares": ...}, ...]
    tran_date_str: "2026-04-30" 或 None
    """
    fund_id = FIRST_FINANCIAL_ACTIVE_ETFS.get(etf_code)
    if not fund_id:
        print(f"  [ERROR] 未知的 ETF 代號: {etf_code}")
        return [], None

    print(f"  抓取 {etf_code} (ID: {fund_id}) 的持股資料...")

    payload = {
        "pStrFundID": fund_id,
        "pStrDate": ""  # 空字串表示抓取最新
    }

    try:
        resp = requests.post(API_URL, json=payload, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        
        result = resp.json()
        # API 回傳格式為 {"d": "[{...}, ...]"}，需要再次解析 JSON 字串
        data_str = result.get("d")
        if not data_str:
            print(f"  [WARN] API 未回傳資料內容")
            return [], None
            
        raw_holdings = json.loads(data_str)
        if not raw_holdings:
            print(f"  [WARN] 持股清單為空")
            return [], None

        holdings = []
        tran_date = None

        for item in raw_holdings:
            # group "1" 通常是股票
            if item.get("group") != "1":
                continue

            # 取得資料日期 (所有項目的 sdate 應該是一樣的)
            if not tran_date:
                tran_date = item.get("sdate")

            code = item.get("A", "").strip()
            name = item.get("B", "").strip().replace(" ", "")
            weight_str = item.get("C", "0")
            shares_str = item.get("D", "0").replace(",", "")

            try:
                weight = round(float(weight_str), 2)
                shares = int(float(shares_str))
            except (ValueError, TypeError):
                continue

            if weight <= 0 and shares <= 0:
                continue

            entry = {
                "name": name,
                "weight": weight,
                "shares": shares if shares > 0 else None,
            }

            # 判斷是否為台股代號 (通常 4-6 位數字)
            if code.isdigit() and 4 <= len(code) <= 6:
                entry["code"] = code
            elif code:
                entry["foreignCode"] = code

            holdings.append(entry)

        return holdings, tran_date

    except Exception as e:
        print(f"  [ERROR] 抓取失敗: {e}")
        return [], None


def _clean_snapshot(h: dict) -> dict:
    """只保留歷史快照應有的欄位。"""
    clean: dict = {"name": h["name"], "weight": h["weight"]}
    if h.get("shares") is not None:
        clean["shares"] = h["shares"]
    if h.get("code"):
        clean["code"] = h["code"]
    if h.get("foreignCode"):
        clean["foreignCode"] = h["foreignCode"]
    return clean


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

        # 檢查資料是否有變化
        if current_date == prev_date and prev_holdings:
            _key = lambda x: x.get("code") or x.get("foreignCode") or x.get("name", "")
            if sorted([_clean_snapshot(h) for h in holdings], key=_key) == sorted([_clean_snapshot(h) for h in prev_holdings], key=_key):
                print(f"  [SKIP] {etf_code} 數據無變化（{current_date}），跳過寫入")
                return True

        if "holdingsHistory" not in data:
            data["holdingsHistory"] = {}

        # 將目前 topHoldings 存入歷史 (若日期不同)
        if prev_date and prev_holdings and prev_date not in data["holdingsHistory"]:
            data["holdingsHistory"][prev_date] = [_clean_snapshot(h) for h in prev_holdings]

        # 計算權重與股數變化
        prev_map = {h.get("code") or h.get("foreignCode") or h.get("name"): h for h in prev_holdings}
        for h in holdings:
            key = h.get("code") or h.get("foreignCode") or h.get("name")
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
        data["holdingsHistory"][current_date] = [_clean_snapshot(h) for h in holdings]

        # 歷史資料保留最近 30 筆
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
        return False


def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(FIRST_FINANCIAL_ACTIVE_ETFS.keys())
    
    success, failed = 0, []

    for i, etf_code in enumerate(targets):
        if etf_code not in FIRST_FINANCIAL_ACTIVE_ETFS:
            print(f"  [SKIP] 不支援的 ETF：{etf_code}")
            continue
            
        print(f"\n[{i+1}/{len(targets)}] {etf_code}")

        holdings, tran_date = fetch_holdings(etf_code)
        if holdings:
            if update_etf_json(etf_code, holdings, tran_date):
                success += 1
            else:
                failed.append(etf_code)
        else:
            failed.append(etf_code)

        if i < len(targets) - 1:
            time.sleep(1)

    print(f"\n第一金投信主動 ETF 更新完成 — 成功: {success}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
