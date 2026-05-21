# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
# ]
# ///
"""
fetch_active_etf_taishin.py

從台新投信官網爬取主動型 ETF 每日持股明細，
更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

原理：
  持股資料直接內嵌於 HTML 頁面（無需另呼叫 API），
  解析 /ETF/Home/ETFSeriesDetail/{code} 的持股表格。

支援 ETF：
  00986A  台新全球龍頭成長主動式ETF基金
  00987A  台新台灣優勢成長主動式ETF基金

用法：
    uv run etf_Crawler/fetch_active_etf_taishin.py          # 更新全部
    uv run etf_Crawler/fetch_active_etf_taishin.py 00987A   # 更新指定
    uv run etf_Crawler/fetch_active_etf_taishin.py 00986A 00987A  # 更新多檔
"""

import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("缺少依賴，請先執行：uv add requests")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

BASE_URL = "https://www.tsit.com.tw/ETF/Home/ETFSeriesDetail"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

TAISHIN_ACTIVE_ETFS = [
    "00986A",  # 台新全球龍頭成長主動式ETF基金
    "00987A",  # 台新台灣優勢成長主動式ETF基金
]


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
    爬取持股頁面，回傳 (holdings, data_date)。
    holdings: [{"name": ..., "weight": ..., "code": ..., "shares": ...}, ...]
    data_date: "2026-04-30" 或 None
    """
    url = f"{BASE_URL}/{etf_code}"
    print(f"  抓取 {url}", end=" ... ", flush=True)

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=45)
            resp.raise_for_status()
            html = resp.text

            m = re.search(r'id="PUB_DATE"[^>]+value="([^"]+)"', html)
            data_date = m.group(1) if m else None

            idx = html.find('<th>代號</th>')
            if idx == -1:
                print(f"找不到持股表格（第 {attempt}/{max_attempts} 次）")
                if attempt < max_attempts:
                    time.sleep(attempt * 10)
                    continue
                return [], data_date

            section = html[idx:idx + 30000]

            rows = re.findall(
                r'<tr>\s*<td>([^<]+)</td>\s*<td>([^<]+)</td>\s*<td>([^<]+)</td>\s*<td>([^<]+)</td>\s*</tr>',
                section,
            )

            holdings = []
            for code_raw, name, shares_raw, weight_raw in rows:
                code_raw = code_raw.strip()
                name = name.strip()
                shares_raw = shares_raw.strip()
                weight_raw = weight_raw.strip()

                try:
                    weight = round(float(weight_raw.rstrip("%").replace(",", "")), 2)
                except (ValueError, TypeError):
                    continue
                if weight <= 0:
                    continue

                try:
                    shares = int(shares_raw.replace(",", ""))
                except (ValueError, TypeError):
                    shares = None

                entry: dict = {"name": name, "weight": weight, "shares": shares}

                parts = code_raw.split()
                numeric = parts[0] if parts else ""
                if numeric.isdigit() and 4 <= len(numeric) <= 6:
                    entry["code"] = numeric
                elif code_raw:
                    entry["foreignCode"] = code_raw

                holdings.append(entry)

            print(f"{len(holdings)} 筆，日期 {data_date}")
            return holdings, data_date

        except Exception as e:
            print(f"失敗: {e}（第 {attempt}/{max_attempts} 次）")
            if attempt < max_attempts:
                time.sleep(attempt * 10)

    return [], None


def _clean_snapshot(h: dict) -> dict:
    """只保留歷史快照應有的欄位，去除比較欄位。"""
    clean: dict = {"name": h["name"], "weight": h["weight"]}
    if h.get("shares") is not None:
        clean["shares"] = h["shares"]
    if h.get("code"):
        clean["code"] = h["code"]
    if h.get("foreignCode"):
        clean["foreignCode"] = h["foreignCode"]
    return clean


def update_etf_json(etf_code: str, holdings: list, data_date: Optional[str]) -> bool:
    json_path = ETF_DATA_DIR / f"{etf_code}.json"
    if not json_path.exists():
        print(f"  [WARN] 找不到 {json_path.name}")
        return False

    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        if not data_date:
            print(f"  [SKIP] 無法取得資料日期，跳過寫入")
            return False

        prev_holdings = data.get("topHoldings", [])
        prev_date = data.get("lastUpdated")
        current_date = data_date

        if current_date == prev_date and prev_holdings:
            _key = lambda x: x.get("code") or x.get("foreignCode") or x.get("name", "")
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
        prev_map = {
            h.get("code") or h.get("foreignCode") or h.get("name"): h
            for h in prev_holdings
        }
        for h in holdings:
            key = h.get("code") or h.get("foreignCode") or h.get("name")
            prev = prev_map.get(key)
            if prev:
                prev_w = prev.get("weight", 0)
                h["previousWeight"] = prev_w
                h["weightChange"] = round(h["weight"] - prev_w, 2)
                prev_s = prev.get("shares") or 0
                h["previousShares"] = prev_s
                h["sharesChange"] = (
                    (h["shares"] - prev_s) if h["shares"] is not None and prev_s else 0
                )
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

        print(f"  [OK] {etf_code} — {len(holdings)} 筆持股，資料日期 {current_date}")
        return True
    except Exception as e:
        print(f"  [ERROR] 寫入失敗: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(TAISHIN_ACTIVE_ETFS)
    unknown = [t for t in targets if t not in TAISHIN_ACTIVE_ETFS]
    if unknown:
        print(f"[ERROR] 不支援的 ETF：{', '.join(unknown)}")
        print(f"支援清單：{', '.join(TAISHIN_ACTIVE_ETFS)}")
        sys.exit(1)

    success, failed = 0, []

    for i, etf_code in enumerate(targets):
        print(f"\n[{i+1}/{len(targets)}] {etf_code}")
        holdings, data_date = fetch_holdings(etf_code)
        if holdings:
            if update_etf_json(etf_code, holdings, data_date):
                success += 1
            else:
                failed.append(etf_code)
        else:
            failed.append(etf_code)

        if i < len(targets) - 1:
            time.sleep(1)

    print(f"\n台新投信主動 ETF 更新完成 — 成功: {success}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
