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
            th = tr.find("th")
            td = tr.find("td")
            if th and td:
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


def fetch_holdings(etf_code: str) -> list:
    """從 MoneyDJ 爬取 ETF 成份股"""
    url = MONEYDJ_HOLDINGS_URL.format(code=etf_code)
    print(f"  抓取成份股 {url}")

    try:
        resp = session.get(url, headers=HEADERS, timeout=15, verify=False)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        table = soup.find("table", {"class": "datalist"})
        if table is None:
            for t in soup.find_all("table"):
                if "投資比例" in t.get_text():
                    table = t
                    break

        if table is None:
            return []

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
            if code_raw:
                entry["code"] = code_raw
            holdings.append(entry)
        return holdings
    except Exception as e:
        print(f"  [WARN] 無法抓取成份股: {e}")
        return []


def update_etf_json(etf_code: str) -> bool:
    json_path = ETF_DATA_DIR / f"{etf_code}.json"
    if not json_path.exists():
        print(f"  [WARN] 找不到 {json_path.name}")
        return False

    try:
        holdings = fetch_holdings(etf_code)
        metadata = fetch_metadata(etf_code)

        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        if holdings:
            data["topHoldings"] = holdings
        if metadata:
            for key in ["dividendFrequency", "inceptionDate", "issuer"]:
                if key in metadata:
                    data[key] = metadata[key]

        data["lastUpdated"] = date.today().isoformat()

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"  [OK] {etf_code} — 更新完成")
        return True
    except Exception as e:
        print(f"  [ERROR] {etf_code} 更新失敗: {e}")
        return False


def load_all_codes() -> list:
    """掃描 src/data/etf/ 下所有 ETF 代碼（排除 index.json 與主動型 ETF）"""
    return sorted(
        p.stem for p in ETF_DATA_DIR.glob("*.json")
        if p.stem != "index" and not p.stem.endswith("A")
    )


def main():
    target_codes = sys.argv[1:] if len(sys.argv) > 1 else load_all_codes()
    active = [c for c in target_codes if c.endswith("A")]
    if active:
        print(f"[WARN] 跳過主動型 ETF（由專屬爬蟲處理）：{', '.join(active)}")
        target_codes = [c for c in target_codes if not c.endswith("A")]
    total = len(target_codes)
    success_count = 0
    failed_codes = []

    print(f"準備更新 {total} 支 ETF")

    for i, code in enumerate(target_codes):
        print(f"\n[{i+1}/{total}] {code}")
        if update_etf_json(code):
            success_count += 1
        else:
            failed_codes.append(code)

        if i < total - 1:
            time.sleep(0.5)

    summary = f"ETF 更新報告 ({date.today().isoformat()})\n"
    summary += f"- 成功: {success_count}/{total}\n"
    if failed_codes:
        summary += f"- 失敗: {', '.join(failed_codes)}\n"

    print(f"\n{summary}")


if __name__ == "__main__":
    main()
