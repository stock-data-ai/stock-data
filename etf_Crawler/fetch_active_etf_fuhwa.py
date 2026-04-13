# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
#   "pandas",
#   "beautifulsoup4",
#   "lxml",
# ]
# ///
"""
fetch_active_etf_fuhwa.py

從復華投信官網爬取主動型 ETF 每日持股明細，
更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

支援 ETF：
  00991A (fundId=ETF23) 復華台灣未來50主動式ETF基金

用法：
    uv run etf_Crawler/fetch_active_etf_fuhwa.py          # 更新全部
    uv run etf_Crawler/fetch_active_etf_fuhwa.py 00991A   # 更新指定
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple

try:
    import requests
    import pandas as pd
    from bs4 import BeautifulSoup
except ImportError:
    print("缺少依賴，請先執行：uv add requests pandas beautifulsoup4 lxml")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

# 復華投信主動型 ETF 對照表：ETF代號 → 官網內部的 fundId (用於 URL)
FUHWA_ACTIVE_ETFS = {
    "00991A": "ETF23",  # 復華台灣未來50主動式ETF基金
}

BASE_URL = "https://www.fhtrust.com.tw/ETF/etf_detail/{fund_id}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
}

def _clean_snapshot(h: dict) -> dict:
    """只保留歷史快照應有的欄位，去除比較欄位。"""
    clean: dict = {"name": h["name"], "weight": h["weight"]}
    if h.get("shares") is not None:
        clean["shares"] = h["shares"]
    if h.get("code"):
        clean["code"] = h["code"]
    return clean

def fetch_holdings(etf_code: str, fund_id: str) -> Tuple[List[dict], Optional[str]]:
    """
    回傳 (holdings, tran_date_str)
    holdings: [{"name": ..., "weight": ..., "code": ..., "shares": ...}, ...]
    tran_date_str: "2026-04-10" 或 None
    """
    url = BASE_URL.format(fund_id=fund_id)
    print(f"  抓取 {url}")

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        
        # 解析日期：通常在頁面上方或表格標題附近
        # 復華的頁面中，淨值表格的第一列通常有日期
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 尋找日期，復華頁面通常有 <span class="date"> 或類似結構
        # 根據之前的 debug 輸出，Table 0 是淨值表，包含「日期」
        tables = pd.read_html(resp.text)
        
        tran_date = None
        holdings = []
        
        # 尋找日期：優先找頁面上的 span.date 或類似
        import re
        tran_date = None
        
        # 嘗試從 HTML 原始碼中尋找日期格式 yyyy/mm/dd
        all_dates = re.findall(r'[0-9]{4}/[0-9]{2}/[0-9]{2}', resp.text)
        if all_dates:
            # 取出現次數最多的日期，通常是資料日期
            from collections import Counter
            common_date = Counter(all_dates).most_common(1)[0][0]
            tran_date = common_date.replace('/', '-')
            print(f"  [DEBUG] Regex found date: {tran_date}")

        if not tran_date:
            date_span = soup.find("span", class_="date")
            if date_span:
                raw_date = date_span.get_text(strip=True).replace('資料日期：', '').replace('/', '-')
                try:
                    tran_date = datetime.strptime(raw_date, '%Y-%m-%d').strftime('%Y-%m-%d')
                except:
                    tran_date = None

        if not tran_date and len(tables) > 0:
            # 備援：從淨值表抓取 (如果 Table 0 的第一列資料是日期)
            df_nav = tables[0]
            if '日期' in df_nav.columns:
                # 有些頁面可能是 2026/04/10，有些可能只顯示部分
                raw_date = str(df_nav.iloc[0]['日期']).strip().replace('/', '-')
                if len(raw_date) >= 8:
                    try:
                        tran_date = datetime.strptime(raw_date, '%Y-%m-%d').strftime('%Y-%m-%d')
                    except:
                        # 嘗試其他格式
                        for fmt in ['%Y-%m-%d', '%Y-%m']:
                            try:
                                tran_date = datetime.strptime(raw_date, fmt).strftime('%Y-%m-%d')
                                break
                            except:
                                continue

        # 尋找持股表格
        holdings_df = None
        for i, df in enumerate(tables):
            # 統一欄位名稱，去除空格
            cols_clean = [str(c).replace(' ', '').replace('\n', '') for c in df.columns]
            if all(c in cols_clean for c in ['證券代號', '證券名稱', '權重(%)']):
                holdings_df = df
                # 同步更新 df 的 columns 以利後續讀取
                df.columns = cols_clean
                break
        
        if holdings_df is not None:
            for _, row in holdings_df.iterrows():
                code = str(row['證券代號']).strip()
                name = str(row['證券名稱']).strip()
                weight_raw = str(row['權重(%)']).replace('%', '').strip()
                shares_raw = str(row['股數']).replace(',', '').strip()
                
                try:
                    weight = round(float(weight_raw), 2)
                except:
                    continue
                
                if weight <= 0:
                    continue
                
                try:
                    shares = int(float(shares_raw))
                except:
                    shares = None
                
                entry = {
                    "name": name,
                    "weight": weight,
                    "shares": shares,
                }
                
                # 台股代號處理
                if code.isdigit() and 4 <= len(code) <= 6:
                    entry["code"] = code
                
                holdings.append(entry)
        
        print(f"  [DEBUG] Found {len(holdings)} holdings records.")
        print(f"  [DEBUG] Final tran_date: {tran_date}")
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
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(FUHWA_ACTIVE_ETFS.keys())
    unknown = [t for t in targets if t not in FUHWA_ACTIVE_ETFS]
    if unknown:
        print(f"[ERROR] 不支援的 ETF：{', '.join(unknown)}")
        sys.exit(1)

    success, failed = 0, []

    for i, etf_code in enumerate(targets):
        fund_id = FUHWA_ACTIVE_ETFS[etf_code]
        print(f"\n[{i+1}/{len(targets)}] {etf_code}")

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

    print(f"\n復華投信主動 ETF 更新完成 — 成功: {success}/{len(targets)}")

if __name__ == "__main__":
    main()
