# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
# ]
# ///
"""
fetch_active_etf_allianz.py

從安聯投信官網 API 爬取主動型 ETF 每日持股明細，
更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

原理：
  網站為 Vue SPA，API 受 ASP.NET Core Antiforgery 保護。
  先呼叫 /webapi/api/AntiForgery/GetAntiForgeryToken 取得 XSRF cookie，
  再以 x-xsrf-token 標頭呼叫 /webapi/api/Fund/GetFundAssets。

支援 ETF：
  00984A (FundID=E0001) 安聯台灣高息主動式ETF基金
  00993A (FundID=E0002) 安聯台灣智慧主動式ETF基金

用法：
    uv run etf_Crawler/fetch_active_etf_allianz.py          # 更新全部
    uv run etf_Crawler/fetch_active_etf_allianz.py 00993A   # 更新指定
"""

import json
import sys
import time
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("缺少依賴，請先執行：uv add requests")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

BASE_URL = "https://etf.allianzgi.com.tw"
ANTIFORGERY_URL = f"{BASE_URL}/webapi/api/AntiForgery/GetAntiForgeryToken"
API_URL = f"{BASE_URL}/webapi/api/Fund/GetFundAssets"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
    "Referer": f"{BASE_URL}/",
    "Origin": BASE_URL,
}

# 安聯投信主動型 ETF 對照表：ETF代號 → FundID
ALLIANZ_ACTIVE_ETFS = {
    "00984A": "E0001",  # 安聯台灣高息主動式ETF基金
    "00993A": "E0002",  # 安聯台灣智慧主動式ETF基金
}


def _create_session() -> tuple[requests.Session, str]:
    """建立 session 並取得 XSRF token。回傳 (session, xsrf_token)。"""
    session = requests.Session()
    resp = session.get(ANTIFORGERY_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    xsrf = session.cookies.get("X-XSRF-TOKEN", "")
    return session, xsrf


def fetch_holdings(etf_code: str, fund_id: str,
                   session: requests.Session, xsrf: str) -> tuple:
    """
    回傳 (holdings, tran_date_str)
    holdings: [{"name": ..., "weight": ..., "code": ..., "shares": ...}, ...]
    tran_date_str: "2026-04-13" 或 None
    """
    print(f"  抓取 {API_URL}  FundID={fund_id}")

    try:
        resp = session.post(
            API_URL,
            json={"FundID": fund_id},
            headers={
                **HEADERS,
                "Content-Type": "application/json",
                "x-xsrf-token": xsrf,
                "Referer": f"{BASE_URL}/etf-info/{fund_id}",
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()

        entries = data.get("Entries", {})
        fund_data = entries.get("Data", {})
        fund_asset = fund_data.get("FundAsset", {}) or {}

        # 解析資料日期
        nav_date_raw = fund_asset.get("NavDate", "")
        tran_date: Optional[str] = None
        if nav_date_raw:
            parts = nav_date_raw.split("/")
            if len(parts) == 3:
                tran_date = f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"

        # 找股票持股表格（TableTitle 以「股票」開頭）
        tables = fund_data.get("Table", [])
        stock_table = next(
            (t for t in tables if str(t.get("TableTitle", "")).startswith("股票") and t.get("Rows")),
            None,
        )

        if not stock_table:
            print(f"  [WARN] 無股票持股資料")
            return [], tran_date

        # 欄位順序：[序號, 股票代號, 股票名稱, 股數, 權重(%)]
        holdings = []
        for row in stock_table["Rows"]:
            if len(row) < 5:
                continue
            code_raw = str(row[1]).strip()
            name = str(row[2]).strip()
            shares_raw = str(row[3]).replace(",", "").strip()
            weight_raw = str(row[4]).replace("%", "").strip()

            try:
                weight = round(float(weight_raw), 2)
            except (ValueError, TypeError):
                continue
            if weight <= 0:
                continue

            try:
                shares = int(float(shares_raw)) if shares_raw and shares_raw != "-" else None
            except (ValueError, TypeError):
                shares = None

            entry: dict = {"name": name, "weight": weight, "shares": shares}
            # 台股代號：4-6 位純數字
            if code_raw.isdigit() and 4 <= len(code_raw) <= 6:
                entry["code"] = code_raw

            holdings.append(entry)

        print(f"  [OK] {len(holdings)} 筆持股，日期 {tran_date}")
        return holdings, tran_date

    except Exception as e:
        print(f"  [ERROR] 抓取失敗: {e}")
        return [], None


def _clean_snapshot(h: dict) -> dict:
    """只保留歷史快照應有的欄位，去除比較欄位。"""
    clean: dict = {"name": h["name"], "weight": h["weight"]}
    if h.get("shares") is not None:
        clean["shares"] = h["shares"]
    if h.get("code"):
        clean["code"] = h["code"]
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
            _key = lambda x: x.get("code") or x.get("name", "")
            if sorted([_clean_snapshot(h) for h in holdings], key=_key) == sorted(
                [_clean_snapshot(h) for h in prev_holdings], key=_key
            ):
                print(f"  [SKIP] {etf_code} 數據無變化（{current_date}），跳過寫入")
                return True

        if "holdingsHistory" not in data:
            data["holdingsHistory"] = {}

        # 舊資料補存歷史
        if prev_date and prev_holdings and prev_date not in data["holdingsHistory"]:
            data["holdingsHistory"][prev_date] = [_clean_snapshot(h) for h in prev_holdings]

        # 計算比較欄位
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

        # 保留最近 30 天
        sorted_dates = sorted(data["holdingsHistory"].keys(), reverse=True)
        for old_d in sorted_dates[30:]:
            del data["holdingsHistory"][old_d]

        data["lastUpdated"] = current_date

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"  [SAVED] {etf_code} — {len(holdings)} 筆持股，資料日期 {current_date}")
        return True
    except Exception as e:
        print(f"  [ERROR] 寫入失敗: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(ALLIANZ_ACTIVE_ETFS.keys())
    unknown = [t for t in targets if t not in ALLIANZ_ACTIVE_ETFS]
    if unknown:
        print(f"[ERROR] 不支援的 ETF：{', '.join(unknown)}")
        print(f"支援清單：{', '.join(ALLIANZ_ACTIVE_ETFS.keys())}")
        sys.exit(1)

    print("取得安聯投信 XSRF token …")
    try:
        session, xsrf = _create_session()
        if not xsrf:
            print("[ERROR] 無法取得 XSRF token，終止")
            sys.exit(1)
        print(f"  token 取得成功")
    except Exception as e:
        print(f"[ERROR] 初始化失敗: {e}")
        sys.exit(1)

    success, failed = 0, []

    for i, etf_code in enumerate(targets):
        fund_id = ALLIANZ_ACTIVE_ETFS[etf_code]
        print(f"\n[{i+1}/{len(targets)}] {etf_code}  (FundID={fund_id})")

        holdings, tran_date = fetch_holdings(etf_code, fund_id, session, xsrf)
        if holdings:
            if update_etf_json(etf_code, holdings, tran_date):
                success += 1
            else:
                failed.append(etf_code)
        else:
            failed.append(etf_code)

        if i < len(targets) - 1:
            time.sleep(1)

    print(f"\n安聯投信主動 ETF 更新完成 — 成功: {success}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")


if __name__ == "__main__":
    main()
