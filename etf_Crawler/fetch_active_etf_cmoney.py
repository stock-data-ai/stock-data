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

import json
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

# stock_map 的 index.json 是 ETF 清單的唯一真相源，本地優先、CI 讀同步過來的那份。
STOCK_MAP_INDEX_LOCAL = REPO_ROOT.parent / "stock_map" / "src" / "data" / "etf" / "index.json"
ETF_INDEX_PATH = ETF_DATA_DIR / "index.json"


def load_active_codes() -> list:
    """
    動態取得主動型 ETF 代號（結尾 A），來源是 index.json。

    這裡刻意不寫死清單：主動型被 fetch_etf_holdings.py 的週更明確排除
    （那支只跑非 A 結尾），日更又只認各腳本自己的硬編碼名單，
    所以清單漏掉一檔 = 那檔永遠不會更新，而且沒有任何地方會報錯。
    2026-09-01 查出 00401A 停 125 天、00989A 停 144 天，就是從沒被加進來過；
    同時還有 6 檔已上市的主動型從頭到尾沒收錄。
    改成讀 index.json 之後，新主動型只要進 index.json 就自動有日更兜底。
    """
    for path in (STOCK_MAP_INDEX_LOCAL, ETF_INDEX_PATH):
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return sorted(e["code"] for e in json.load(f) if e["code"].endswith("A"))
    print("[WARN] 找不到 ETF index.json，主動型清單為空")
    return []


CMONEY_ACTIVE_ETFS = load_active_codes()


def _load_index_map() -> dict:
    for path in (STOCK_MAP_INDEX_LOCAL, ETF_INDEX_PATH):
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return {e["code"]: e for e in json.load(f)}
    return {}


def ensure_skeleton(etf_code: str) -> None:
    """
    {code}.json 不存在就從 index.json 建空骨架。

    write_holdings_update() 遇到檔案不存在只會印 [WARN] 然後回 False，
    不會建檔也不會讓流程失敗——新上市 ETF 會就這樣安靜地一直抓不到。
    """
    json_path = ETF_DATA_DIR / f"{etf_code}.json"
    if json_path.exists():
        return
    entry = _load_index_map().get(etf_code, {})
    skeleton = {
        "code": etf_code,
        "name": entry.get("name", etf_code),
        "assetClass": entry.get("assetClass"),
        "categoryId": entry.get("categoryId"),
        "trackingIndex": entry.get("trackingIndex"),
        "managementFee": entry.get("managementFee"),
        "dividendFrequency": entry.get("dividendFrequency"),
        "inceptionDate": None,
        "fundSize": entry.get("fundSize"),
        "issuer": entry.get("issuer"),
        "description": None,
        "topHoldings": [],
        "holdingsHistory": {},
        "lastUpdated": None,
    }
    ETF_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(skeleton, f, ensure_ascii=False, indent=2)
    print(f"  [New] 建立骨架 {etf_code}.json")

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
        ensure_skeleton(etf_code)
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
        ensure_skeleton(etf_code)
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
