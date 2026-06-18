# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
# ]
# ///
"""
fetch_active_etf_ctbc.py

從中國信託投信官網 API 爬取主動型 ETF 每日持股明細，
更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

原理：
  網站使用 Vue SPA，資料來自後端 REST API (ctbcinvestments.com.tw/API)。
  先呼叫 home/AuthToken 取得 session token，
  再呼叫 etf/ETFHoldingWeight 取得持股清單。

支援 ETF：
  00406A  中國信託台灣收益成長主動式ETF基金
  00983A  中國信託ARK創新主動式ETF基金
  00995A  中國信託台灣卓越成長主動式ETF基金

用法：
    uv run etf_Crawler/fetch_active_etf_ctbc.py          # 更新全部
    uv run etf_Crawler/fetch_active_etf_ctbc.py 00406A   # 更新指定
"""

import json
import sys
import time
import urllib.parse
from datetime import date, timedelta
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

API_BASE = "https://www.ctbcinvestments.com.tw/API"
INITIAL_TOKEN = "www.ctbcinvestments.com.tw"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json; charset=utf-8",
    "Referer": "https://www.ctbcinvestments.com.tw/",
    "Origin": "https://www.ctbcinvestments.com.tw",
}

# 中信投信主動型 ETF 清單：{ETF代號: FID}
# FID 可由 etf/ETFList API 查得
CTBC_ACTIVE_ETFS = {
    "00406A": "E0038",  # 中國信託台灣收益成長主動式ETF基金
    "00983A": "E0034",  # 中國信託ARK創新主動式ETF基金
    "00995A": "E0036",  # 中國信託台灣卓越成長主動式ETF基金
}

session = create_session()


def _get_auth_token() -> Optional[str]:
    """呼叫 home/AuthToken 取得 session token。"""
    url = f"{API_BASE}/home/AuthToken"
    encoded = urllib.parse.quote(INITIAL_TOKEN)
    try:
        resp = session.post(
            url,
            params={"token": INITIAL_TOKEN},
            json={"token": INITIAL_TOKEN},
            headers=HEADERS,
            timeout=45,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ResultCode") == 0:
            return data["Data"]["token"]
        print(f"  [WARN] AuthToken 回傳錯誤: {data.get('ResultMsg')}")
        return None
    except Exception as e:
        print(f"  [ERROR] 取得 token 失敗: {e}")
        return None


def fetch_holdings(etf_code: str, fid: str, token: str) -> tuple:
    """
    呼叫 etf/ETFHoldingWeight API，回傳 (holdings, data_date)。
    holdings: [{"name": ..., "weight": ..., "code": ..., "shares": ...}, ...]
    data_date: "2026-04-09" 或 None
    """
    # 預設查詢昨日（若今日尚未更新則往前找最近一筆）
    query_date = (date.today() - timedelta(days=1)).isoformat()
    url = f"{API_BASE}/etf/ETFHoldingWeight"

    print(f"  抓取 {url}  FID={fid}  StartDate={query_date}")

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.post(
                url,
                params={"token": token},
                json={"token": token, "FID": fid, "StartDate": query_date},
                headers=HEADERS,
                timeout=45,
            )
            resp.raise_for_status()
            result = resp.json()

            if result.get("ResultCode") != 0:
                print(f"  [WARN] API 錯誤: {result.get('ResultMsg')}（第 {attempt}/{max_attempts} 次）")
                if attempt < max_attempts:
                    time.sleep(attempt * 10)
                    continue
                return [], None

            api_data = result["Data"]

            fund_assets = api_data.get("FundAssets", [])
            data_date: Optional[str] = None
            if fund_assets:
                raw_dt = fund_assets[0].get("NAV_DT", "")
                if "T" in raw_dt:
                    data_date = raw_dt.split("T")[0]
                elif "/" in raw_dt:
                    data_date = raw_dt.replace("/", "-")
                else:
                    data_date = raw_dt or None

            holdings = []
            for section in api_data.get("FundAssetsDetail", []):
                if section.get("Code") != "STOCK":
                    continue
                for item in section.get("Data", []):
                    code_raw = str(item.get("code_", "")).strip()
                    name = str(item.get("name_", "")).strip()
                    weight_str = str(item.get("weights_", "")).replace(",", "").strip()
                    qty_str = str(item.get("qty_", "")).replace(",", "").strip()

                    try:
                        weight = round(float(weight_str), 2)
                    except (ValueError, TypeError):
                        continue
                    if weight <= 0:
                        continue

                    try:
                        shares = int(float(qty_str)) if qty_str and qty_str != "0.00" else None
                    except (ValueError, TypeError):
                        shares = None

                    entry: dict = {"name": name, "weight": weight, "shares": shares}

                    if code_raw.isdigit() and 4 <= len(code_raw) <= 6:
                        entry["code"] = code_raw
                    elif code_raw:
                        entry["foreignCode"] = code_raw

                    holdings.append(entry)

            if not holdings:
                print(f"  [WARN] 無股票持股資料（第 {attempt}/{max_attempts} 次）")
                if attempt < max_attempts:
                    time.sleep(attempt * 10)
                    continue
                return [], data_date

            holdings.sort(key=lambda h: h["weight"], reverse=True)
            return holdings, data_date

        except Exception as e:
            print(f"  [ERROR] 抓取失敗: {e}（第 {attempt}/{max_attempts} 次）")
            if attempt < max_attempts:
                time.sleep(attempt * 10)

    return [], None

def update_etf_json(etf_code: str, holdings: list, tran_date: Optional[str]) -> bool:
    return write_holdings_update(
        ETF_DATA_DIR / f"{etf_code}.json",
        etf_code, holdings, tran_date,
        has_foreign_code=True,
    )



def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(CTBC_ACTIVE_ETFS)
    unknown = [t for t in targets if t not in CTBC_ACTIVE_ETFS]
    if unknown:
        print(f"[ERROR] 不支援的 ETF：{', '.join(unknown)}")
        print(f"支援清單：{', '.join(CTBC_ACTIVE_ETFS)}")
        sys.exit(1)

    print("取得中信投信 API token …")
    token = _get_auth_token()
    if not token:
        print("[ERROR] 無法取得 token，終止")
        sys.exit(1)
    print(f"  token 取得成功")

    success, failed, unchanged = 0, [], 0
    results: dict = {}

    for i, etf_code in enumerate(targets):
        fid = CTBC_ACTIVE_ETFS[etf_code]
        print(f"\n[{i+1}/{len(targets)}] {etf_code}  (FID={fid})")

        holdings, data_date = fetch_holdings(etf_code, fid, token)
        if holdings:
            result = update_etf_json(etf_code, holdings, data_date)
            if result is True:
                success += 1
                results[etf_code] = ("updated", data_date or "")
            elif result == "unchanged":
                unchanged += 1
                results[etf_code] = ("unchanged", data_date or "")
            else:
                failed.append(etf_code)
                results[etf_code] = ("failed", "")
        else:
            failed.append(etf_code)
            results[etf_code] = ("failed", "")

        if i < len(targets) - 1:
            time.sleep(1)

    write_github_output(results)
    print(f"\n中信投信主動 ETF 更新完成 — 已更新: {success}，無變化: {unchanged}，失敗: {len(failed)}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
