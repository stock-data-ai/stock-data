# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
# ]
# ///
"""
fetch_active_etf_nomura.py

從野村投信官網 API 爬取主動型 ETF 每日持股明細，
更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

支援 ETF：
  00980A  野村臺灣優選成長主動式ETF基金
  00985A  野村台灣50主動式ETF基金
  00999A  野村臺灣高息主動式ETF基金

API：POST https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundAssets
     Body: {"FundID": "<code>", "SearchDate": null | "YYYY-MM-DD"}

用法：
    uv run etf_Crawler/fetch_active_etf_nomura.py                        # 更新全部（最新）
    uv run etf_Crawler/fetch_active_etf_nomura.py 00980A                 # 更新指定（最新）
    uv run etf_Crawler/fetch_active_etf_nomura.py --date 2026-04-08      # 指定日期
    uv run etf_Crawler/fetch_active_etf_nomura.py --backfill 7           # 補過去 N 天歷史
    uv run etf_Crawler/fetch_active_etf_nomura.py 00985A --backfill 7    # 指定 ETF 補歷史
"""

import json
import sys
import time
from datetime import date, timedelta
from etf_utils import create_session, write_holdings_update
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("缺少依賴，請先執行：uv add requests")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

API_URL = "https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundAssets"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer": "https://www.nomurafunds.com.tw/ETFWEB/",
}

NOMURA_ACTIVE_ETFS = [
    "00980A",  # 野村臺灣優選成長主動式ETF基金
    "00985A",  # 野村台灣50主動式ETF基金
    "00999A",  # 野村臺灣高息主動式ETF基金
]

STATUS_NO_DATA = 5  # API 回傳「此搜尋條件尚無相關資料」

session = create_session()


def _parse_date(nav_date: str) -> Optional[str]:
    """將 '2026/04/09' 轉為 '2026-04-09'。"""
    try:
        parts = nav_date.split("/")
        return f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
    except Exception:
        return None

def fetch_holdings(etf_code: str, search_date: Optional[str] = None) -> tuple:
    """
    回傳 (holdings, tran_date_str)
    search_date: "YYYY-MM-DD" 或 None（最新）
    非交易日回傳 ([], None)
    """
    label = search_date or "最新"
    print(f"  抓取 {etf_code} [{label}]", end=" ... ", flush=True)

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.post(
                API_URL,
                headers=HEADERS,
                json={"FundID": etf_code, "SearchDate": search_date},
                timeout=45,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("StatusCode") == STATUS_NO_DATA:
                print("無資料（非交易日）")
                return [], None

            entries = data.get("Entries", {})
            fund_data = entries.get("Data", {})
            fund_asset = fund_data.get("FundAsset", {}) or {}
            tables = fund_data.get("Table", [])

            tran_date = _parse_date(fund_asset.get("NavDate", ""))

            stock_table = next(
                (t for t in tables if t.get("TableTitle") == "股票"), None
            )
            if not stock_table or not stock_table.get("Rows"):
                print(f"無股票持股資料（第 {attempt}/{max_attempts} 次）")
                if attempt < max_attempts:
                    time.sleep(attempt * 10)
                    continue
                return [], None

            holdings = []
            for row in stock_table["Rows"]:
                if len(row) < 4:
                    continue
                code_raw = str(row[0]).strip()
                name = str(row[1]).strip()
                shares_raw = row[2]
                weight_raw = row[3]

                try:
                    weight = round(float(weight_raw), 2)
                except (ValueError, TypeError):
                    continue
                if weight <= 0:
                    continue

                try:
                    shares = int(str(shares_raw).replace(",", ""))
                except (ValueError, TypeError):
                    shares = None

                entry: dict = {"name": name, "weight": weight, "shares": shares}
                if code_raw.isdigit() and 4 <= len(code_raw) <= 6:
                    entry["code"] = code_raw

                holdings.append(entry)

            print(f"{len(holdings)} 筆，日期 {tran_date}")
            return holdings, tran_date

        except Exception as e:
            print(f"失敗: {e}（第 {attempt}/{max_attempts} 次）")
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



def _has_shares(etf_code: str, dt: str) -> bool:
    """檢查指定日期的 holdingsHistory 是否含有張數資料。"""
    json_path = ETF_DATA_DIR / f"{etf_code}.json"
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        snap = data.get("holdingsHistory", {}).get(dt, [])
        return any(h.get("shares") is not None for h in snap)
    except Exception:
        return False


def run_backfill(targets: list, days: int, force: bool = False) -> None:
    """補過去 N 天的 holdingsHistory。
    force=True 時：即使日期已存在，若無張數資料也重新抓取覆蓋。
    """
    today = date.today()
    dates = [
        (today - timedelta(days=i)).isoformat()
        for i in range(days, 0, -1)  # 由舊到新
    ]

    print(f"\n補歷史模式：過去 {days} 天（{dates[0]} ～ {dates[-1]}）{'[強制覆蓋無張數資料]' if force else ''}")

    for etf_code in targets:
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
                if force and not _has_shares(etf_code, d):
                    print(f"  {d} — 已有資料但無張數，重新抓取")
                else:
                    print(f"  {d} — 已有資料，跳過")
                    skipped += 1
                    continue

            holdings, tran_date = fetch_holdings(etf_code, search_date=d)
            if holdings:
                update_etf_json(etf_code, holdings, tran_date, history_only=True)
                filled += 1
            time.sleep(0.5)

        print(f"  補完：{filled} 天新增，{skipped} 天已存在")


def _parse_args():
    """解析 sys.argv，回傳 (targets, search_date, backfill_days)。"""
    args = sys.argv[1:]
    search_date = None
    backfill_days = None
    etf_codes = []

    force = False
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
        elif args[i] == "--force":
            force = True
            i += 1
        elif args[i].startswith("--"):
            print(f"[ERROR] 未知選項：{args[i]}")
            sys.exit(1)
        else:
            etf_codes.append(args[i])
            i += 1

    targets = etf_codes if etf_codes else list(NOMURA_ACTIVE_ETFS)
    unknown = [t for t in targets if t not in NOMURA_ACTIVE_ETFS]
    if unknown:
        print(f"[ERROR] 不支援的 ETF：{', '.join(unknown)}")
        print(f"支援清單：{', '.join(NOMURA_ACTIVE_ETFS)}")
        sys.exit(1)

    return targets, search_date, backfill_days, force


def main():
    targets, search_date, backfill_days, force = _parse_args()

    if backfill_days is not None:
        run_backfill(targets, backfill_days, force=force)
        return

    success, failed = 0, []

    for i, etf_code in enumerate(targets):
        print(f"\n[{i+1}/{len(targets)}] {etf_code}")
        holdings, tran_date = fetch_holdings(etf_code, search_date=search_date)
        if holdings:
            if update_etf_json(etf_code, holdings, tran_date):
                success += 1
            else:
                failed.append(etf_code)
        else:
            failed.append(etf_code)

        if i < len(targets) - 1:
            time.sleep(1)

    print(f"\n野村投信主動 ETF 更新完成 — 成功: {success}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
