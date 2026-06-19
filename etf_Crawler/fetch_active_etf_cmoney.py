# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
# ]
# ///
"""
fetch_active_etf_cmoney.py

透過 CMoney API 抓取所有主動型 ETF 每日持股，作為各投信官網的第二次補漏來源。
每天 19:30 跑，補足 16:00 官網爬蟲未更新的 ETF。

API: https://www.cmoney.tw/api/cm/MobileService/ashx/GetDtnoData.ashx
     DtNo=59449513, MajorTable=M722

用法:
    uv run python etf_Crawler/fetch_active_etf_cmoney.py          # 全部
    uv run python etf_Crawler/fetch_active_etf_cmoney.py 00991A   # 指定
"""

import sys
import time
from datetime import date
from etf_utils import create_session, write_github_output, write_holdings_update
from pathlib import Path
from typing import Optional

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

CMONEY_ACTIVE_ETFS = [
    "00403A", "00981A", "00988A",  # 統一
    "00982A", "00992A", "00997A",  # 群益
    "00980A", "00985A", "00999A",  # 野村
    "00990A",                       # 元大
    "00406A", "00983A", "00995A",  # 中信
    "00991A", "00998A",            # 復華
    "00984A", "00993A",            # 安聯
    "00986A", "00987A",            # 台新
    "00994A",                       # 第一金
    "00996A",                       # 兆豐
    "00400A",                       # 國泰
    "00405A",                       # 富邦
]

session = create_session()


def _parse_rows(rows: list) -> dict:
    """把 API rows 依日期分組，回傳 {date_str: [holdings]}，日期由舊到新排序。"""
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


def fetch_holdings(etf_code: str, dtrange: int = 1) -> tuple:
    """回傳 (holdings, tran_date)，只取最新一天。"""
    all_dates = fetch_holdings_all_dates(etf_code, dtrange)
    if not all_dates:
        return [], None
    latest = max(all_dates)
    return all_dates[latest], latest


def fetch_holdings_all_dates(etf_code: str, dtrange: int = 30) -> dict:
    """回傳 {date_str: [holdings]} for DTRange 個交易日。"""
    print(f"  抓取 CMoney ({etf_code}, DTRange={dtrange})", end=" ... ", flush=True)
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
        print(f"{len(by_date)} 個交易日，{min(by_date)} ～ {max(by_date)}")
        return by_date
    except Exception as e:
        print(f"失敗: {e}")
        return {}


def needs_update(etf_code: str, tran_date: str) -> bool:
    """
    判斷是否需要用 CMoney 資料更新本地 JSON。

    規則：
    1. 本地日期為未來日期（官網誤植） → 強制用 CMoney 修正
    2. CMoney 日期 > 本地日期 → 補漏，寫入
    3. CMoney 日期 <= 本地日期 → 本地已是最新或更新，跳過
    """
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
            print(f"  [WARN] {etf_code} 本地日期 {last} 為未來日期，CMoney（{tran_date}）覆蓋修正")
            return True
        return tran_date > last
    except Exception:
        return True


def run_backfill(targets: list, dtrange: int = 30) -> None:
    """從 CMoney 抓過去 dtrange 個交易日，清洗並重寫所有 ETF JSON。"""
    print(f"\n=== CMoney Backfill 模式：過去 {dtrange} 個交易日 ===\n")
    failed = []
    for i, etf_code in enumerate(targets):
        print(f"\n[{i+1}/{len(targets)}] {etf_code}")
        all_dates = fetch_holdings_all_dates(etf_code, dtrange)
        if not all_dates:
            failed.append(etf_code)
            if i < len(targets) - 1:
                time.sleep(0.5)
            continue

        sorted_dates = sorted(all_dates.keys())
        latest_date = sorted_dates[-1]

        # 歷史日期：只補 holdingsHistory
        for d in sorted_dates[:-1]:
            write_holdings_update(
                ETF_DATA_DIR / f"{etf_code}.json",
                etf_code, all_dates[d], d,
                history_only=True,
            )

        # 最新日期：完整寫入（更新 topHoldings + lastUpdated，修正異常日期）
        write_holdings_update(
            ETF_DATA_DIR / f"{etf_code}.json",
            etf_code, all_dates[latest_date], latest_date,
        )

        if i < len(targets) - 1:
            time.sleep(0.5)

    print(f"\nBackfill 完成 — 失敗: {failed or '無'}")
    if failed:
        sys.exit(1)


def main():
    args = sys.argv[1:]

    # --backfill 模式
    if "--backfill" in args:
        args = [a for a in args if a != "--backfill"]
        targets = args if args else CMONEY_ACTIVE_ETFS
        unknown = [t for t in targets if t not in CMONEY_ACTIVE_ETFS]
        if unknown:
            print(f"[ERROR] 不支援的 ETF：{', '.join(unknown)}")
            sys.exit(1)
        run_backfill(targets)
        return

    targets = args if args else CMONEY_ACTIVE_ETFS
    unknown = [t for t in targets if t not in CMONEY_ACTIVE_ETFS]
    if unknown:
        print(f"[ERROR] 不支援的 ETF：{', '.join(unknown)}")
        sys.exit(1)

    success, failed, skipped = 0, [], 0
    results: dict = {}

    for i, etf_code in enumerate(targets):
        print(f"\n[{i+1}/{len(targets)}] {etf_code}")
        holdings, tran_date = fetch_holdings(etf_code)

        if not holdings:
            failed.append(etf_code)
            results[etf_code] = ("failed", "")
            if i < len(targets) - 1:
                time.sleep(0.3)
            continue

        if not needs_update(etf_code, tran_date):
            print(f"  [SKIP] {etf_code} 已是最新（{tran_date}），略過")
            skipped += 1
            results[etf_code] = ("unchanged", tran_date)
            if i < len(targets) - 1:
                time.sleep(0.3)
            continue

        result = write_holdings_update(
            ETF_DATA_DIR / f"{etf_code}.json",
            etf_code, holdings, tran_date,
        )
        if result is True:
            success += 1
            results[etf_code] = ("updated", tran_date)
        elif result == "unchanged":
            skipped += 1
            results[etf_code] = ("unchanged", tran_date)
        else:
            failed.append(etf_code)
            results[etf_code] = ("failed", "")

        if i < len(targets) - 1:
            time.sleep(0.3)

    # 用 CMONEY_ 前綴輸出，讓 Job Summary 可與官網日期對比
    gho = __import__("os").environ.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a", encoding="utf-8") as f:
            for code, (status, tran_date) in results.items():
                f.write(f"CMONEY_{code}_STATUS={status}\n")
                f.write(f"CMONEY_{code}_DATE={tran_date}\n")

    print(f"\nCMoney 補漏完成 — 已更新: {success}，略過: {skipped}，失敗: {len(failed)}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
