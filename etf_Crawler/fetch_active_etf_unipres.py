# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
#   "beautifulsoup4",
# ]
# ///
"""
fetch_active_etf_unipres.py

從統一投信官網 (ezmoney.com.tw) 爬取主動型 ETF 每日持股明細，
更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

支援 ETF：
  00981A (fundCode=49YTW) 主動統一台股增長
  00988A (fundCode=61YTW) 主動統一全球創新

用法：
    uv run etf_Crawler/fetch_active_etf_unipres.py          # 更新全部
    uv run etf_Crawler/fetch_active_etf_unipres.py 00981A   # 更新指定
"""

import json
import sys
import html as html_module
import time
from datetime import date
from pathlib import Path
from typing import Optional

try:
    import requests
    import urllib3
    from bs4 import BeautifulSoup
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    print("缺少依賴，請先執行：uv add requests beautifulsoup4")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

BASE_URL = "https://www.ezmoney.com.tw/ETF/Fund/Info"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}

# 統一投信主動型 ETF 對照表：ETF代號 → ezmoney fundCode
UNIPRES_ACTIVE_ETFS = {
    "00981A": "49YTW",
    "00988A": "61YTW",
}


def create_session() -> requests.Session:
    session = requests.Session()
    # 先 GET 一次拿 cookie（網站有 302 + cookie 防護）
    session.get(BASE_URL, headers=HEADERS, timeout=15, verify=False)
    return session


def _clean_snapshot(h: dict) -> dict:  # type: ignore[return]
    """只保留歷史快照應有的欄位，去除比較欄位。"""
    clean: dict = {"name": h["name"], "weight": h["weight"]}
    if h.get("shares") is not None:
        clean["shares"] = h["shares"]
    if h.get("code"):
        clean["code"] = h["code"]
    if h.get("foreignCode"):
        clean["foreignCode"] = h["foreignCode"]
    return clean


def fetch_holdings(session: requests.Session, etf_code: str, fund_code: str) -> tuple:
    """
    回傳 (holdings, tran_date_str)
    holdings: [{"name": ..., "weight": ..., "code": ...}, ...]
    tran_date_str: "2026-04-08" 或 None
    """
    url = f"{BASE_URL}?fundCode={fund_code}&tabName=asset"
    print(f"  抓取 {url}")

    try:
        resp = session.get(url, headers=HEADERS, timeout=15, verify=False)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        elem = soup.find(id="DataAsset")
        if not elem:
            print(f"  [WARN] 找不到 DataAsset 元素")
            return [], None

        raw = html_module.unescape(elem.get("data-content", ""))
        asset_data: list[dict] = json.loads(raw)

        # 找股票資產區塊
        stock_block = next((a for a in asset_data if a.get("AssetCode") == "ST"), None)
        if not stock_block or not stock_block.get("Details"):
            print(f"  [WARN] 無股票持股資料")
            return [], None

        details = stock_block["Details"]
        tran_date = details[0].get("TranDate", "")[:10] if details else None

        holdings = []
        for d in details:
            code_raw = str(d.get("DetailCode", "")).strip()
            name = str(d.get("DetailName", "")).strip()
            weight = d.get("NavRate")
            shares = d.get("Share")

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
                "shares": int(shares) if shares is not None else None
            }
            # 台股代號：4-6位純數字
            if code_raw.isdigit() and 4 <= len(code_raw) <= 6:
                entry["code"] = code_raw
            elif code_raw:
                entry["foreignCode"] = code_raw

            holdings.append(entry)

        return holdings, tran_date

    except Exception as e:
        print(f"  [ERROR] 抓取失敗: {e}")
        return [], None


def update_etf_json(etf_code: str, holdings: list[dict], tran_date: Optional[str]) -> bool:
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

        # 如果有舊資料且尚未存入歷史，先補存（只存乾淨欄位）
        if prev_date and prev_holdings and prev_date not in data["holdingsHistory"]:
            data["holdingsHistory"][prev_date] = [_clean_snapshot(h) for h in prev_holdings]

        # 計算 topHoldings 的比較欄位（相對於昨日快照）
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

        # 歷史快照只存乾淨欄位（不含比較欄位）
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
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(UNIPRES_ACTIVE_ETFS.keys())
    unknown = [t for t in targets if t not in UNIPRES_ACTIVE_ETFS]
    if unknown:
        print(f"[ERROR] 不支援的 ETF：{', '.join(unknown)}")
        print(f"支援清單：{', '.join(UNIPRES_ACTIVE_ETFS.keys())}")
        sys.exit(1)

    session = create_session()
    success, failed = 0, []

    for i, etf_code in enumerate(targets):
        fund_code = UNIPRES_ACTIVE_ETFS[etf_code]
        print(f"\n[{i+1}/{len(targets)}] {etf_code} (fundCode={fund_code})")

        holdings, tran_date = fetch_holdings(session, etf_code, fund_code)
        if holdings:
            if update_etf_json(etf_code, holdings, tran_date):
                success += 1
            else:
                failed.append(etf_code)
        else:
            failed.append(etf_code)

        if i < len(targets) - 1:
            time.sleep(1)

    print(f"\n統一投信主動 ETF 更新完成 — 成功: {success}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")


if __name__ == "__main__":
    main()
