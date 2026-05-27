# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
#   "pandas",
#   "beautifulsoup4",
#   "lxml",
#   "openpyxl",
# ]
# ///
"""
fetch_active_etf_fuhwa.py

從復華投信官網爬取主動型 ETF 每日持股明細，
更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

策略：HTML 取最新日期與 Excel 連結 → Excel API 取完整持股（非前10筆）。
      指定日期時直接組 Excel API URL，不需過 HTML。

支援 ETF：
  00991A (fundId=ETF23) 復華台灣未來50主動式ETF基金
  00998A (fundId=ETF24) 復華全球金融股息主動式ETF基金

用法：
    uv run etf_Crawler/fetch_active_etf_fuhwa.py                       # 更新全部（最新）
    uv run etf_Crawler/fetch_active_etf_fuhwa.py 00991A                # 更新指定（最新）
    uv run etf_Crawler/fetch_active_etf_fuhwa.py --date 2026-04-28     # 指定日期
    uv run etf_Crawler/fetch_active_etf_fuhwa.py --backfill 7          # 補過去 N 天
    uv run etf_Crawler/fetch_active_etf_fuhwa.py 00998A --backfill 7   # 指定 ETF 補歷史
"""

import io
import json
import sys
import time
from datetime import date, timedelta
from etf_utils import create_session, write_holdings_update
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import requests
    import pandas as pd
    from bs4 import BeautifulSoup
except ImportError:
    print("缺少依賴，請先執行：uv add requests pandas beautifulsoup4 lxml openpyxl")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

FUHWA_ACTIVE_ETFS = {
    "00991A": "ETF23",  # 復華台灣未來50主動式ETF基金
    "00998A": "ETF24",  # 復華全球金融股息主動式ETF基金
}

BASE_URL = "https://www.fhtrust.com.tw/ETF/etf_detail/{fund_id}"
EXCEL_URL = "https://www.fhtrust.com.tw/api/assetsExcel/{fund_id}/{date_compact}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
}

session = create_session()

def _parse_excel(content: bytes) -> List[dict]:
    """從 Excel bytes 解析持股清單（跳過頁首、期貨段）。"""
    df_raw = pd.read_excel(io.BytesIO(content), header=None)

    header_row = None
    for i, row in df_raw.iterrows():
        if any(str(v).strip() == '證券代號' for v in row):
            header_row = i
            break

    if header_row is None:
        return []

    df_raw.columns = [str(v).strip() for v in df_raw.iloc[header_row]]
    df_stocks = df_raw.iloc[header_row + 1:].reset_index(drop=True)

    holdings = []
    for _, row in df_stocks.iterrows():
        code_raw = str(row.get('證券代號', '')).strip()
        name = str(row.get('證券名稱', '')).strip()
        weight_raw = str(row.get('權重(%)', '')).replace('%', '').strip()
        shares_raw = str(row.get('股數', '')).replace(',', '').strip()

        if not name or name in ('nan', '期貨名稱'):
            break

        try:
            weight = round(float(weight_raw), 2)
        except (ValueError, TypeError):
            continue

        if weight <= 0:
            continue

        try:
            shares = int(float(shares_raw))
        except (ValueError, TypeError):
            shares = None

        entry: dict = {"name": name, "weight": weight, "shares": shares}
        if code_raw.replace(' ', '').isdigit() and 4 <= len(code_raw.replace(' ', '')) <= 6:
            entry["code"] = code_raw.replace(' ', '')

        holdings.append(entry)

    return holdings


def fetch_holdings(etf_code: str, fund_id: str, search_date: Optional[str] = None) -> Tuple[List[dict], Optional[str]]:
    """
    回傳 (holdings, tran_date_str)。
    search_date: "YYYY-MM-DD" 或 None（從 HTML 取最新日期）。
    """
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            if search_date:
                date_compact = search_date.replace('-', '')
                tran_date = search_date
                print(f"  指定日期: {tran_date}")
            else:
                page_url = BASE_URL.format(fund_id=fund_id)
                print(f"  抓取 {page_url}")
                resp = session.get(page_url, headers=HEADERS, timeout=45)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, 'html.parser')

                excel_link = soup.find('a', href=lambda h: h and 'assetsExcel' in h)
                if not excel_link:
                    print(f"  [ERROR] 找不到 Excel 下載連結（第 {attempt}/{max_attempts} 次）")
                    if attempt < max_attempts:
                        time.sleep(attempt * 10)
                        continue
                    return [], None

                date_compact = excel_link['href'].split('/')[-1]
                tran_date = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}"
                print(f"  資料日期: {tran_date}")

            excel_url = EXCEL_URL.format(fund_id=fund_id, date_compact=date_compact)
            print(f"  下載 {excel_url}")
            xresp = session.get(excel_url, headers=HEADERS, timeout=45)
            xresp.raise_for_status()

            if len(xresp.content) < 100:
                print(f"  [SKIP] {tran_date} 無資料（可能為假日）")
                return [], None

            holdings = _parse_excel(xresp.content)
            if not holdings:
                print(f"  [ERROR] Excel 中找不到持股表頭（第 {attempt}/{max_attempts} 次）")
                if attempt < max_attempts:
                    time.sleep(attempt * 10)
                    continue
                return [], None

            print(f"  找到 {len(holdings)} 筆持股")
            return holdings, tran_date

        except Exception as e:
            print(f"  [ERROR] 抓取失敗: {e}（第 {attempt}/{max_attempts} 次）")
            if attempt < max_attempts:
                time.sleep(attempt * 10)

    return [], None


def update_etf_json(etf_code: str, holdings: list, tran_date: Optional[str],
                    history_only: bool = False) -> bool:
    return write_holdings_update(
        ETF_DATA_DIR / f"{etf_code}.json",
        etf_code, holdings, tran_date,
        history_only=history_only,
    )



def run_backfill(targets: list, days: int) -> None:
    today = date.today()
    dates = [
        (today - timedelta(days=i)).isoformat()
        for i in range(days, 0, -1)
    ]
    print(f"\n補歷史模式：過去 {days} 天（{dates[0]} ～ {dates[-1]}）")

    for etf_code in targets:
        fund_id = FUHWA_ACTIVE_ETFS[etf_code]
        print(f"\n【{etf_code}】")
        json_path = ETF_DATA_DIR / f"{etf_code}.json"
        if not json_path.exists():
            print(f"  [WARN] 找不到 {json_path.name}，跳過")
            continue

        with open(json_path, encoding="utf-8") as f:
            existing = json.load(f)
        existing_dates = set(existing.get("holdingsHistory", {}).keys())

        filled, skipped = 0, 0
        for d in dates:
            if d in existing_dates:
                print(f"  {d} — 已有資料，跳過")
                skipped += 1
                continue

            holdings, tran_date = fetch_holdings(etf_code, fund_id, search_date=d)
            if holdings:
                update_etf_json(etf_code, holdings, tran_date, history_only=True)
                filled += 1
            time.sleep(0.5)

        print(f"  補完：{filled} 天新增，{skipped} 天已存在")


def _parse_args():
    args = sys.argv[1:]
    search_date = None
    backfill_days = None
    etf_codes = []

    i = 0
    while i < len(args):
        if args[i] == "--date" and i + 1 < len(args):
            search_date = args[i + 1]
            i += 2
        elif args[i] == "--backfill" and i + 1 < len(args):
            try:
                backfill_days = int(args[i + 1])
            except ValueError:
                print(f"[ERROR] --backfill 需要整數，收到：{args[i+1]}")
                sys.exit(1)
            i += 2
        elif args[i].startswith("--"):
            print(f"[ERROR] 未知選項：{args[i]}")
            sys.exit(1)
        else:
            etf_codes.append(args[i])
            i += 1

    targets = etf_codes if etf_codes else list(FUHWA_ACTIVE_ETFS.keys())
    unknown = [t for t in targets if t not in FUHWA_ACTIVE_ETFS]
    if unknown:
        print(f"[ERROR] 不支援的 ETF：{', '.join(unknown)}")
        print(f"支援清單：{', '.join(FUHWA_ACTIVE_ETFS)}")
        sys.exit(1)

    return targets, search_date, backfill_days


def main():
    targets, search_date, backfill_days = _parse_args()

    if backfill_days is not None:
        run_backfill(targets, backfill_days)
        return

    success, failed = 0, []
    for i, etf_code in enumerate(targets):
        fund_id = FUHWA_ACTIVE_ETFS[etf_code]
        print(f"\n[{i+1}/{len(targets)}] {etf_code}")
        holdings, tran_date = fetch_holdings(etf_code, fund_id, search_date=search_date)
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
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
