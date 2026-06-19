# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
# ]
# ///
"""
fetch_etf_holdings_cmoney.py

透過 CMoney API 抓取被動型 ETF 每日持股，取代 MoneyDJ 爬蟲。
從 src/data/etf/ 目錄自動偵測所有非主動型 ETF（排除 *A 結尾）。

不支援的 ETF（CMoney 無持股資料）：
  00625K 00636K 00643K 00657K 00668K（境外掛牌型，自動跳過）

用法：
    uv run python etf_Crawler/fetch_etf_holdings_cmoney.py          # 全部
    uv run python etf_Crawler/fetch_etf_holdings_cmoney.py 0050     # 指定
    uv run python etf_Crawler/fetch_etf_holdings_cmoney.py --backfill  # 補 30 天歷史
"""

import sys
import time
from datetime import date
from pathlib import Path
from etf_utils import create_session, write_holdings_update

try:
    import requests
except ImportError:
    print("缺少依賴，請先執行：uv add requests")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

API_URL = "https://www.cmoney.tw/api/cm/MobileService/ashx/GetDtnoData.ashx"
DTNO = "59449513"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.cmoney.tw/etf/tw/",
}

# CMoney 無持股資料（境外掛牌型）
UNSUPPORTED = {"00625K", "00636K", "00643K", "00657K", "00668K"}

session = create_session()


def all_passive_codes() -> list:
    """讀取 data 目錄中所有被動型 ETF 代號（排除主動型 *A 與不支援）。"""
    codes = sorted(
        p.stem for p in ETF_DATA_DIR.glob("*.json")
        if not p.stem.endswith("A") and p.stem != "index" and p.stem not in UNSUPPORTED
    )
    return codes


def _parse_rows(rows: list) -> dict:
    """把 API rows 依日期分組，回傳 {date_str: [holdings]}，由舊到新排序。"""
    from collections import defaultdict
    by_date: dict = defaultdict(list)
    for row in rows:
        raw_date = str(row[0])
        tran_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
        code_raw = str(row[1]).strip()
        name = str(row[2]).strip()
        try:
            weight = round(float(row[3]), 2)
        except (ValueError, TypeError):
            continue
        if weight <= 0:
            continue
        try:
            shares = int(float(row[4])) if row[4] else None
        except (ValueError, TypeError):
            shares = None
        entry: dict = {"name": name, "weight": weight, "shares": shares}
        if code_raw.isdigit() and 4 <= len(code_raw) <= 6:
            entry["code"] = code_raw
        elif code_raw:
            entry["foreignCode"] = code_raw
        by_date[tran_date].append(entry)
    return dict(sorted(by_date.items()))


def fetch_all_dates(etf_code: str, dtrange: int = 1) -> dict:
    """回傳 {date_str: [holdings]}。dtrange=1 只取最新一天。"""
    print(f"  CMoney ({etf_code}, DTRange={dtrange})", end=" ... ", flush=True)
    try:
        resp = session.get(
            API_URL,
            params={
                "action": "getdtnodata",
                "DtNo": DTNO,
                "ParamStr": f"AssignID={etf_code};MTPeriod=0;DTMode=0;DTRange={dtrange};DTOrder=1;MajorTable=M722;",
                "FilterNo": "0",
            },
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        rows = resp.json().get("Data", [])
        if not rows:
            print("無資料")
            return {}
        by_date = _parse_rows(rows)
        latest = max(by_date)
        print(f"{len(by_date[latest])} 筆，{latest}")
        return by_date
    except Exception as e:
        print(f"失敗: {e}")
        return {}


def needs_update(etf_code: str, tran_date: str) -> bool:
    json_path = ETF_DATA_DIR / f"{etf_code}.json"
    if not json_path.exists():
        return True
    try:
        import json
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        last = data.get("lastUpdated", "")
        if not last:
            return True
        today = date.today().isoformat()
        if last > today:
            print(f"  [WARN] {etf_code} 本地日期 {last} 為未來日期，覆蓋修正")
            return True
        return tran_date > last
    except Exception:
        return True


def run_backfill(targets: list) -> None:
    print(f"\n=== CMoney Backfill 模式（被動型 ETF，DTRange=30）===\n")
    failed = []
    for i, etf_code in enumerate(targets):
        print(f"\n[{i+1}/{len(targets)}] {etf_code}")
        all_dates = fetch_all_dates(etf_code, dtrange=30)
        if not all_dates:
            failed.append(etf_code)
            if i < len(targets) - 1:
                time.sleep(0.3)
            continue
        sorted_dates = sorted(all_dates.keys())
        for d in sorted_dates[:-1]:
            write_holdings_update(ETF_DATA_DIR / f"{etf_code}.json", etf_code, all_dates[d], d, history_only=True)
        write_holdings_update(ETF_DATA_DIR / f"{etf_code}.json", etf_code, all_dates[sorted_dates[-1]], sorted_dates[-1])
        if i < len(targets) - 1:
            time.sleep(0.3)
    print(f"\nBackfill 完成 — 失敗: {failed or '無'}")
    if failed:
        sys.exit(1)


def main():
    args = sys.argv[1:]

    if "--backfill" in args:
        args = [a for a in args if a != "--backfill"]
        targets = args if args else all_passive_codes()
        unknown = [t for t in targets if t not in all_passive_codes() and t not in UNSUPPORTED]
        if unknown:
            print(f"[ERROR] 找不到對應 JSON：{', '.join(unknown)}")
            sys.exit(1)
        run_backfill([t for t in targets if t not in UNSUPPORTED])
        return

    targets = args if args else all_passive_codes()
    skipped_unsupported = [t for t in targets if t in UNSUPPORTED]
    targets = [t for t in targets if t not in UNSUPPORTED]

    if skipped_unsupported:
        print(f"[INFO] CMoney 不支援，跳過：{', '.join(skipped_unsupported)}")

    success, failed, skipped = 0, [], 0

    for i, etf_code in enumerate(targets):
        print(f"\n[{i+1}/{len(targets)}] {etf_code}")
        all_dates = fetch_all_dates(etf_code)
        if not all_dates:
            failed.append(etf_code)
            if i < len(targets) - 1:
                time.sleep(0.3)
            continue

        latest = max(all_dates)
        holdings = all_dates[latest]

        if not needs_update(etf_code, latest):
            print(f"  [SKIP] 已是最新（{latest}）")
            skipped += 1
            if i < len(targets) - 1:
                time.sleep(0.3)
            continue

        result = write_holdings_update(ETF_DATA_DIR / f"{etf_code}.json", etf_code, holdings, latest)
        if result is True:
            success += 1
        elif result == "unchanged":
            skipped += 1
        else:
            failed.append(etf_code)

        if i < len(targets) - 1:
            time.sleep(0.3)

    print(f"\n完成 — 已更新: {success}，略過: {skipped}，失敗: {len(failed)}/{len(targets)}")
    if failed:
        print(f"失敗清單: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
