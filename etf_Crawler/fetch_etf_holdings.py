# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
#   "beautifulsoup4",
# ]
# ///
"""
fetch_etf_holdings.py

從 MoneyDJ 爬取 ETF 成份股與持股比例，並同步更新基本資料。
更新欄位：
- src/data/etf/{code}.json (topHoldings, dividendFrequency, managementFee, inceptionDate, fundSize, issuer)

用法：
    uv run etf_Crawler/fetch_etf_holdings.py              # 更新全部
    uv run etf_Crawler/fetch_etf_holdings.py 0050 0056    # 更新指定代碼
"""

import json
import sys
import re
import time
from datetime import date
from pathlib import Path

try:
    import requests
    import urllib3
    from bs4 import BeautifulSoup
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    print("缺少依賴，請先執行：uv add requests beautifulsoup4")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"
MONEYDJ_HOLDINGS_URL = "https://www.moneydj.com/ETF/X/Basic/Basic0007B.xdjhtm?etfid={code}.TW"
MONEYDJ_BASIC_URL = "https://www.moneydj.com/ETF/X/Basic/Basic0004.xdjhtm?etfid={code}.TW"

STOCK_MAP_INDEX_LOCAL = REPO_ROOT.parent / "stock_map" / "src" / "data" / "etf" / "index.json"
ETF_INDEX_PATH = ETF_DATA_DIR / "index.json"  # stock-data 自己的（由 sync-topics.yml 同步）

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}


def create_session():
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

session = create_session()


def fetch_metadata(etf_code: str) -> dict:  # type: ignore[return]
    """從 MoneyDJ 爬取 ETF 基本資料"""
    url = MONEYDJ_BASIC_URL.format(code=etf_code)
    print(f"  抓取基本資料 {url}")

    try:
        resp = session.get(url, headers=HEADERS, timeout=15, verify=False)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        metadata = {}

        for tr in soup.find_all("tr"):
            ths = tr.find_all("th")
            tds = tr.find_all("td")
            for th, td in zip(ths, tds):
                label = th.get_text(strip=True)
                value = td.get_text(strip=True)

                if "配息頻率" in label:
                    metadata["dividendFrequency"] = value
                elif "成立日期" in label:
                    m = re.search(r"(\d{4}/\d{2}/\d{2})", value)
                    if m:
                        metadata["inceptionDate"] = m.group(1).replace("/", "-")
                elif "發行公司" in label:
                    metadata["issuer"] = value
        return metadata
    except Exception as e:
        print(f"  [WARN] 無法抓取基本資料: {e}")
        return {}


def fetch_holdings(etf_code: str) -> tuple[list, str]:
    """從 MoneyDJ 爬取 ETF 成份股與資料日期"""
    url = MONEYDJ_HOLDINGS_URL.format(code=etf_code)
    print(f"  抓取成份股 {url}")

    try:
        resp = session.get(url, headers=HEADERS, timeout=15, verify=False)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        # 抓取資料日期
        data_date = ""
        match = re.search(r"資料日期：(\d{4}/\d{2}/\d{2})", soup.get_text())
        if match:
            data_date = match.group(1).replace("/", "-")

        table = soup.find("table", {"class": "datalist"})
        if table is None:
            for t in soup.find_all("table"):
                if "投資比例" in t.get_text():
                    table = t
                    break

        if table is None:
            return [], data_date

        holdings = []
        rows = table.find_all("tr")
        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            name_cell = cells[0]
            weight_cell = cells[1]
            link = name_cell.find("a")
            code_raw = ""
            if link and link.get("href"):
                m = re.search(r"etfid=(\w+)\.TW", link.get("href", ""))
                if m:
                    code_raw = m.group(1)
            name_text = name_cell.get_text(strip=True)
            name_text = re.sub(r"\s*\(\S+\)\s*$", "", name_text).strip()
            weight_text = weight_cell.get_text(strip=True).replace("%", "").replace(",", "")
            try:
                weight = round(float(weight_text), 2)
            except ValueError:
                continue
            if weight <= 0:
                continue
            entry = {"name": name_text, "weight": weight}
            
            if len(cells) >= 3:
                shares_text = cells[2].get_text(strip=True).replace(",", "")
                if shares_text:
                    try:
                        entry["shares"] = int(float(shares_text))
                    except ValueError:
                        pass
                        
            if code_raw:
                entry["code"] = code_raw
            holdings.append(entry)
        return holdings, data_date
    except Exception as e:
        print(f"  [WARN] 無法抓取成份股: {e}")
        return [], ""


def _clean_snapshot(h: dict) -> dict:
    """只保留歷史快照應有的欄位，去除比較欄位。"""
    clean: dict = {"name": h["name"], "weight": h["weight"]}
    if h.get("shares") is not None:
        clean["shares"] = h["shares"]
    if h.get("code"):
        clean["code"] = h["code"]
    return clean


def update_etf_json(etf_code: str) -> bool:
    json_path = ETF_DATA_DIR / f"{etf_code}.json"
    if not json_path.exists():
        print(f"  [WARN] 找不到 {json_path.name}，嘗試建立骨架...")
        index = _load_stock_map_index()
        entry = next((e for e in index if e["code"] == etf_code), None)
        if entry:
            ensure_skeleton(entry)
        else:
            print(f"  [WARN] index.json 也找不到 {etf_code}，跳過")
            return False

    try:
        holdings, data_date = fetch_holdings(etf_code)
        metadata = fetch_metadata(etf_code)

        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        # 比對 metadata
        if metadata:
            for key in ["dividendFrequency", "inceptionDate", "issuer"]:
                if key in metadata and data.get(key) != metadata[key]:
                    data[key] = metadata[key]

        if not holdings:
            print(f"  [Skip] {etf_code} — 無持股資料")
            # 仍需寫回 metadata（債券 ETF 無成份股但有配息頻率/成立日期）
            if metadata:
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            return True

        current_date = data_date or date.today().isoformat()
        prev_holdings = data.get("topHoldings", [])
        prev_date = data.get("lastUpdated")

        # 比對是否有變動
        if current_date == prev_date and prev_holdings:
            _key = lambda x: x.get("code") or x.get("name", "")
            if sorted([_clean_snapshot(h) for h in holdings], key=_key) == sorted(
                [_clean_snapshot(h) for h in prev_holdings], key=_key
            ):
                print(f"  [Skip] {etf_code} — 資料無變動 ({current_date})")
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
                h["sharesChange"] = (h["shares"] - prev_s) if h.get("shares") is not None and prev_s else 0
            else:
                h["previousWeight"] = 0
                h["weightChange"] = h["weight"]
                h["previousShares"] = 0
                h["sharesChange"] = h.get("shares") or 0

        data["topHoldings"] = holdings
        data["holdingsHistory"][current_date] = [_clean_snapshot(h) for h in holdings]

        # 保留最近 30 天
        sorted_dates = sorted(data["holdingsHistory"].keys(), reverse=True)
        for old_d in sorted_dates[30:]:
            del data["holdingsHistory"][old_d]

        data["lastUpdated"] = current_date

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"  [OK] {etf_code} — 更新完成 ({current_date})")
        return True
    except Exception as e:
        print(f"  [ERROR] {etf_code} 更新失敗: {e}")
        return False


def _load_stock_map_index() -> list[dict]:
    """讀 ETF index.json：本地 stock_map 優先 → 同目錄 index.json（CI 環境）"""
    if STOCK_MAP_INDEX_LOCAL.exists():
        with open(STOCK_MAP_INDEX_LOCAL, encoding="utf-8") as f:
            return json.load(f)
    if ETF_INDEX_PATH.exists():
        with open(ETF_INDEX_PATH, encoding="utf-8") as f:
            return json.load(f)
    print("[WARN] 找不到 ETF index.json")
    return []


def ensure_skeleton(entry: dict) -> None:
    """若 {code}.json 不存在，從 index.json 資料建立空骨架"""
    code = entry["code"]
    json_path = ETF_DATA_DIR / f"{code}.json"
    if json_path.exists():
        return
    skeleton = {
        "code": code,
        "name": entry.get("name", code),
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
    print(f"  [New] 建立骨架 {code}.json")


def load_all_codes() -> list:
    """從 stock_map index.json 讀取全部代碼（排除主動型 ETF）"""
    index = _load_stock_map_index()
    if index:
        return sorted(e["code"] for e in index if not e["code"].endswith("A"))
    # fallback：掃現有檔案
    return sorted(
        p.stem for p in ETF_DATA_DIR.glob("*.json")
        if p.stem != "index" and not p.stem.endswith("A")
    )


def main():
    index = _load_stock_map_index()
    index_map = {e["code"]: e for e in index}

    all_codes = sys.argv[1:] if len(sys.argv) > 1 else load_all_codes()
    active = [c for c in all_codes if c.endswith("A")]
    target_codes = [c for c in all_codes if not c.endswith("A")]
    total = len(target_codes)
    success_count = 0
    failed_codes = []

    # 先建立所有缺少的骨架
    missing = [c for c in target_codes if not (ETF_DATA_DIR / f"{c}.json").exists()]
    if missing:
        print(f"\n建立 {len(missing)} 支新 ETF 骨架...")
        for c in missing:
            if c in index_map:
                ensure_skeleton(index_map[c])

    print(f"\n準備更新 {total} 支 ETF")

    for i, code in enumerate(target_codes):
        print(f"\n[{i+1}/{total}] {code}")
        if update_etf_json(code):
            success_count += 1
        else:
            failed_codes.append(code)

        if i < total - 1:
            time.sleep(0.5)

    # 主動型 ETF：只更新 dividendFrequency / inceptionDate / issuer
    if active:
        print(f"\n準備更新 {len(active)} 支主動型 ETF 配息資料")
        active_ok = 0
        for i, code in enumerate(active):
            json_path = ETF_DATA_DIR / f"{code}.json"
            if not json_path.exists():
                print(f"  [Skip] {code} — 找不到 JSON")
                continue
            print(f"\n[A {i+1}/{len(active)}] {code}")
            metadata = fetch_metadata(code)
            if not metadata:
                print(f"  [Skip] {code} — 無 metadata")
                continue
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
            changed = False
            for key in ["dividendFrequency", "inceptionDate", "issuer"]:
                if key in metadata and data.get(key) != metadata[key]:
                    data[key] = metadata[key]
                    changed = True
            if changed:
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                print(f"  [OK] {code} — metadata 更新")
            else:
                print(f"  [Skip] {code} — 無變動")
            active_ok += 1
            if i < len(active) - 1:
                time.sleep(0.5)

    summary = f"ETF 更新報告 ({date.today().isoformat()})\n"
    summary += f"- 成功: {success_count}/{total}\n"
    if active:
        summary += f"- 主動型 metadata: {len(active)} 支\n"
    if failed_codes:
        summary += f"- 失敗: {', '.join(failed_codes)}\n"

    print(f"\n{summary}")


if __name__ == "__main__":
    main()
