# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
#   "beautifulsoup4",
# ]
# ///
"""
sync_etf_metadata.py

統一同步 ETF 基本資料到 src/data/etf/{code}.json。

更新欄位：
- managementFee
- dividendFrequency
- inceptionDate
- fundSize
- issuer

用法：
    uv run python etf_Crawler/sync_etf_metadata.py
    uv run python etf_Crawler/sync_etf_metadata.py 0050 00982A
"""

import json
import re
import sys
import time
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
MONEYDJ_BASIC_URL = "https://www.moneydj.com/ETF/X/Basic/Basic0004.xdjhtm?etfid={code}.TW"
STOCK_MAP_INDEX_LOCAL = REPO_ROOT.parent / "stock_map" / "src" / "data" / "etf" / "index.json"
ETF_INDEX_PATH = ETF_DATA_DIR / "index.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}


def create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


session = create_session()


def _load_stock_map_index() -> list:
    if STOCK_MAP_INDEX_LOCAL.exists():
        with open(STOCK_MAP_INDEX_LOCAL, encoding="utf-8") as f:
            return json.load(f)
    if ETF_INDEX_PATH.exists():
        with open(ETF_INDEX_PATH, encoding="utf-8") as f:
            return json.load(f)
    print("[WARN] 找不到 ETF index.json")
    return []


def ensure_skeleton(entry: dict) -> None:
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
        "inceptionDate": entry.get("inceptionDate"),
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
        f.write("\n")
    print(f"  [New] 建立骨架 {code}.json")


def load_all_codes() -> list:
    index = _load_stock_map_index()
    if index:
        return sorted(e["code"] for e in index)
    return sorted(p.stem for p in ETF_DATA_DIR.glob("*.json") if p.stem != "index")


def _parse_float(value: str) -> float:
    cleaned = value.replace(",", "").strip()
    return round(float(cleaned), 4)


def fetch_metadata(etf_code: str) -> dict:
    url = MONEYDJ_BASIC_URL.format(code=etf_code)
    print(f"  抓取基本資料 {url}")

    try:
        resp = session.get(url, headers=HEADERS, timeout=15, verify=False)
        resp.raise_for_status()
        resp.encoding = "utf-8"

        text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
        start = text.find("ETF名稱")
        if start != -1:
            text = text[start:]
        end = text.find("附註：")
        if end != -1:
            text = text[:end]
        metadata = {}

        match = re.search(r"配息頻率\s+(\S+)", text)
        if match:
            metadata["dividendFrequency"] = match.group(1)

        match = re.search(r"成立日期\s+(\d{4}/\d{2}/\d{2})", text)
        if match:
            metadata["inceptionDate"] = match.group(1).replace("/", "-")

        match = re.search(r"發行公司\s+(.+?)\s+交易所", text)
        if match:
            metadata["issuer"] = match.group(1).strip()

        match = re.search(r"ETF規模\s+([\d,]+(?:\.\d+)?)\(百萬台幣\)", text)
        if match:
            metadata["fundSize"] = _parse_float(match.group(1))

        match = re.search(r"經理費\(%\)\s+([\d.]+)", text)
        if match:
            metadata["managementFee"] = _parse_float(match.group(1))

        return metadata
    except Exception as e:
        print(f"  [WARN] {etf_code} 無法抓取基本資料: {e}")
        return {}


def update_etf_json(etf_code: str, index_map: dict) -> bool:
    json_path = ETF_DATA_DIR / f"{etf_code}.json"
    if not json_path.exists():
        entry = index_map.get(etf_code)
        if not entry:
            print(f"  [WARN] {etf_code} 不在 index.json，跳過")
            return False
        ensure_skeleton(entry)

    metadata = fetch_metadata(etf_code)
    if not metadata:
        return False

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    changed = False
    for key, value in metadata.items():
        if value in ("", None):
            continue
        if data.get(key) != value:
            data[key] = value
            changed = True

    if changed:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"  [OK] {etf_code} — metadata 更新")
    else:
        print(f"  [Skip] {etf_code} — 無變動")

    return True


def main() -> None:
    index = _load_stock_map_index()
    index_map = {e["code"]: e for e in index}
    codes = sys.argv[1:] if len(sys.argv) > 1 else load_all_codes()

    print(f"準備同步 {len(codes)} 支 ETF 基本資料")

    success = 0
    failed = []
    for i, code in enumerate(codes):
        print(f"\n[{i+1}/{len(codes)}] {code}")
        if update_etf_json(code, index_map):
            success += 1
        else:
            failed.append(code)

        if i < len(codes) - 1:
            time.sleep(0.5)

    print(f"\nETF 基本資料同步完成 — 成功: {success}/{len(codes)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")


if __name__ == "__main__":
    main()
