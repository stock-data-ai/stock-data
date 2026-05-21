# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
#   "beautifulsoup4",
# ]
# ///
"""
fetch_active_etf_mega.py

從兆豐投信官網 (megafunds.com.tw) 爬取主動型 ETF 每日持股明細，
更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

支援 ETF：
  00996A  主動兆豐台灣豐收 (ID: 23)
"""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

try:
    import requests
    from bs4 import BeautifulSoup
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("缺少依賴，請先執行：uv add requests beautifulsoup4")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

BASE_URL = "https://www.megafunds.com.tw/MEGA/etf/etf_product.aspx?id={fund_id}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

# 兆豐投信主動型 ETF 代號與內部 ID 映射
MEGA_ACTIVE_ETFS = {
    "00996A": "23",  # 主動兆豐台灣豐收
}


def create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.5,
        status_forcelist=[408, 429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


session = create_session()


def fetch_holdings(etf_code: str) -> tuple:
    """
    回傳 (holdings, tran_date_str)
    holdings: [{"name": ..., "weight": ..., "code": ..., "shares": ...}, ...]
    tran_date_str: "2026-04-30" 或 None
    """
    fund_id = MEGA_ACTIVE_ETFS.get(etf_code)
    if not fund_id:
        print(f"  [ERROR] 未知的 ETF 代號: {etf_code}")
        return [], None

    url = BASE_URL.format(fund_id=fund_id)
    print(f"  抓取 {etf_code} (URL: {url})...")

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=45)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")

            tran_date = None
            date_pattern = re.compile(r"(\d{4})/(\d{2})/(\d{2})")

            source_tag = soup.find(string=re.compile("資料來源"))
            if source_tag:
                match = date_pattern.search(source_tag)
                if match:
                    tran_date = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

            if not tran_date:
                date_text = soup.find(string=date_pattern)
                if date_text:
                    match = date_pattern.search(date_text)
                    if match:
                        tran_date = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

            holdings_container = soup.find(id="fund_content_list_1")
            if not holdings_container:
                print(f"  [WARN] 找不到持股容器 (fund_content_list_1)（第 {attempt}/{max_attempts} 次）")
                if attempt < max_attempts:
                    time.sleep(attempt * 10)
                    continue
                return [], None

            rows = holdings_container.find_all("div", class_="fund-info")
            if not rows:
                print(f"  [WARN] 找不到持股項目 (fund-info)（第 {attempt}/{max_attempts} 次）")
                if attempt < max_attempts:
                    time.sleep(attempt * 10)
                    continue
                return [], None

            holdings = []
            for row in rows:
                cols = row.find_all("div", class_="fund-content")
                if len(cols) < 4:
                    continue

                code_raw = cols[0].get_text(strip=True)
                name = cols[1].get_text(strip=True)
                shares_raw = cols[2].get_text(strip=True).replace(",", "")
                weight_raw = cols[3].get_text(strip=True).replace("%", "").strip()

                try:
                    weight = round(float(weight_raw), 2)
                    shares = int(float(shares_raw))
                except (ValueError, TypeError):
                    continue

                if weight <= 0:
                    continue

                entry = {
                    "name": name,
                    "weight": weight,
                    "shares": shares if shares > 0 else None,
                }

                if code_raw.isdigit() and 4 <= len(code_raw) <= 6:
                    entry["code"] = code_raw
                elif code_raw:
                    entry["foreignCode"] = code_raw

                holdings.append(entry)

            return holdings, tran_date

        except Exception as e:
            print(f"  [ERROR] 抓取失敗: {e}（第 {attempt}/{max_attempts} 次）")
            if attempt < max_attempts:
                time.sleep(attempt * 10)

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

        if current_date == prev_date and prev_holdings:
            _key = lambda x: x.get("code") or x.get("foreignCode") or x.get("name", "")
            if sorted([_clean_snapshot(h) for h in holdings], key=_key) == sorted([_clean_snapshot(h) for h in prev_holdings], key=_key):
                print(f"  [SKIP] {etf_code} 數據無變化（{current_date}），跳過寫入")
                return True

        if "holdingsHistory" not in data:
            data["holdingsHistory"] = {}

        if prev_date and prev_holdings and prev_date not in data["holdingsHistory"]:
            data["holdingsHistory"][prev_date] = [_clean_snapshot(h) for h in prev_holdings]

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
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(MEGA_ACTIVE_ETFS.keys())
    
    success, failed = 0, []

    for i, etf_code in enumerate(targets):
        if etf_code not in MEGA_ACTIVE_ETFS:
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

    print(f"\n兆豐投信主動 ETF 更新完成 — 成功: {success}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
