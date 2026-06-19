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


def fetch_holdings(etf_code: str) -> tuple:
    """回傳 (holdings, tran_date)。"""
    print(f"  抓取 CMoney API ({etf_code})", end=" ... ", flush=True)
    try:
        resp = session.get(
            API_URL,
            params={
                "action": "getdtnodata",
                "DtNo": DTNO,
                "ParamStr": f"AssignID={etf_code};MTPeriod=0;DTMode=0;DTRange=1;DTOrder=1;MajorTable=M722;",
                "FilterNo": "0",
            },
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("Data", [])
        if not rows:
            print("無資料")
            return [], None

        tran_date_raw = rows[0][0]  # "20260618"
        tran_date = f"{tran_date_raw[:4]}-{tran_date_raw[4:6]}-{tran_date_raw[6:]}"

        holdings = []
        for row in rows:
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
            holdings.append(entry)

        print(f"{len(holdings)} 筆，{tran_date}")
        return holdings, tran_date

    except Exception as e:
        print(f"失敗: {e}")
        return [], None


def needs_update(etf_code: str, tran_date: str) -> bool:
    """檢查本地 JSON 的 lastUpdated 是否已是 tran_date，若已是則不需更新。"""
    json_path = ETF_DATA_DIR / f"{etf_code}.json"
    if not json_path.exists():
        return True
    try:
        import json
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("lastUpdated") != tran_date
    except Exception:
        return True


def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else CMONEY_ACTIVE_ETFS
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
