# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "playwright",
#   "requests",
#   "beautifulsoup4",
# ]
# ///
"""
sync_etf_metadata.py

統一同步 ETF 基本資料到 src/data/etf/{code}.json。

資料來源：
- 玩股網：managementFee, fundSize, issuer, inceptionDate, beneficiaryCount
- 玩股網（若 API 有提供）：trailingYield
- MoneyDJ：dividendFrequency

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
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    print("缺少依賴，請先執行：uv sync --all-extras --dev")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"
MONEYDJ_BASIC_URL = "https://www.moneydj.com/ETF/X/Basic/Basic0004.xdjhtm?etfid={code}.TW"
STOCK_MAP_INDEX_LOCAL = REPO_ROOT.parent / "stock_map" / "src" / "data" / "etf" / "index.json"
ETF_INDEX_PATH = ETF_DATA_DIR / "index.json"

RANKING_URL = "https://www.wantgoo.com/stock/etf/ranking/volume"
DIVIDEND_URL = "https://www.wantgoo.com/stock/etf/dividend"
API_BASIC = "wantgoo.com/stock/etf/basic-data"
API_VALUE = "wantgoo.com/stock/etf/daily-value-data"
API_DIVIDEND = "wantgoo.com/stock/etf/dividend-data"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}

STRIP = re.compile(r"證券投資信託股份有限公司|投資信託股份有限公司|證券投資信託")
ALIAS = {
    "元大": "元大投信", "富邦": "富邦投信", "國泰": "國泰投信",
    "中國信託": "中國信託投信", "永豐": "永豐投信", "群益": "群益投信",
    "台新": "台新投信", "統一": "統一投信", "野村": "野村投信",
    "安聯": "安聯投信", "復華": "復華投信", "凱基": "凱基投信",
    "第一金": "第一金投信", "新光": "新光投信", "華南": "華南投信",
    "合庫": "合庫投信", "玉山": "玉山投信", "日盛": "日盛投信",
    "聯邦": "聯邦投信", "兆豐": "兆豐投信", "台灣工銀": "台灣工銀投信",
    "富蘭克林": "富蘭克林投信", "施羅德": "施羅德投信",
    "摩根": "摩根投信", "貝萊德": "貝萊德投信",
    "聯博": "聯博投信", "霸菱": "霸菱投信", "柏瑞": "柏瑞投信",
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
stealth = Stealth()


def _issuer(manager: str) -> str:
    short = STRIP.sub("", manager).strip()
    for key, val in ALIAS.items():
        if short.startswith(key):
            return val
    return short + ("投信" if short and not short.endswith("投信") else "")


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
        "trailingYield": entry.get("trailingYield"),
        "beneficiaryCount": entry.get("beneficiaryCount"),
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


def wait_for_wantgoo_ready(page) -> None:
    page.wait_for_function(
        """() => {
            const titleOk = !document.title.includes('Just a moment');
            const challenge = document.querySelector('#challenge-error-text');
            return titleOk && !challenge;
        }""",
        timeout=30000,
    )
    page.wait_for_timeout(5000)


def fetch_wantgoo_data() -> tuple:
    max_attempts = 3
    last_err = None

    with sync_playwright() as p:
        for attempt in range(1, max_attempts + 1):
            browser = p.chromium.launch(
                channel="chromium",
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                ctx = browser.new_context(
                    user_agent=HEADERS["User-Agent"],
                    locale="zh-TW",
                    extra_http_headers={
                        "Accept-Language": HEADERS["Accept-Language"],
                        "X-Requested-With": "XMLHttpRequest",
                    },
                )
                stealth.apply_stealth_sync(ctx)
                page = ctx.new_page()
                page.goto(RANKING_URL, wait_until="load", timeout=90000)
                wait_for_wantgoo_ready(page)

                basic_list, value_list, dividend_list = page.evaluate(
                    """async () => {
                        const fetchJson = (path) => new Promise((resolve, reject) => {
                            const xhr = new XMLHttpRequest();
                            xhr.open('GET', path, true);
                            xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
                            xhr.setRequestHeader('Accept', '*/*');
                            xhr.setRequestHeader('Accept-Language', 'zh-TW,zh;q=0.9');
                            xhr.onreadystatechange = () => {
                                if (xhr.readyState !== XMLHttpRequest.DONE) return;
                                if (xhr.status >= 200 && xhr.status < 300) {
                                    try {
                                        resolve(JSON.parse(xhr.responseText));
                                    } catch (err) {
                                        reject(new Error(`${path} => invalid json`));
                                    }
                                } else {
                                    reject(new Error(`${path} => ${xhr.status}`));
                                }
                            };
                            xhr.onerror = () => reject(new Error(`${path} => network error`));
                            xhr.send();
                        });

                        const [basic, value, dividend] = await Promise.all([
                            fetchJson('/stock/etf/basic-data'),
                            fetchJson('/stock/etf/daily-value-data'),
                            fetchJson('/stock/etf/dividend-data'),
                        ]);
                        return [basic, value, dividend];
                    }"""
                )

                if not basic_list or not value_list:
                    raise TimeoutError(
                        f"排行頁 API 未取得有效資料: basic={len(basic_list)} value={len(value_list)} dividend={len(dividend_list)}"
                    )

                return basic_list, value_list, dividend_list
            except Exception as e:
                last_err = e
                print(f"  [attempt {attempt}/{max_attempts}] 玩股網抓取失敗: {e}")
                if attempt < max_attempts:
                    time.sleep(10)
            finally:
                browser.close()

    raise RuntimeError(f"玩股網資料抓取失敗: {last_err}") from last_err


def build_wantgoo_metadata_maps() -> tuple:
    try:
        basic_list, value_list, dividend_list = fetch_wantgoo_data()
        basic_map = {e["stockNo"]: e for e in basic_list}
        value_map = {e["stockNo"]: e for e in value_list}
        dividend_map = {e["stockNo"]: e for e in dividend_list}
        return basic_map, value_map, dividend_map
    except Exception as e:
        print(f"[WARN] 玩股網資料抓取失敗，本次改為沿用既有欄位，只同步配息頻率: {e}")
        return {}, {}, {}


def _latest_beneficiary_count(value: dict) -> int:
    distributions = value.get("distributions") or []
    if not distributions:
        return None
    people = distributions[0].get("people")
    return people or None


def fetch_dividend_frequency(etf_code: str) -> dict:
    url = MONEYDJ_BASIC_URL.format(code=etf_code)
    print(f"  抓取配息頻率 {url}")

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

        VALID_FREQUENCIES = {"月配", "雙月配", "季配", "半年配", "年配", "不配息"}
        match = re.search(r"配息頻率\s+(\S+)", text)
        if match and match.group(1) in VALID_FREQUENCIES:
            return {"dividendFrequency": match.group(1)}
        return {}
    except Exception as e:
        print(f"  [WARN] {etf_code} 無法抓取配息頻率: {e}")
        return {}


def build_metadata_for_code(etf_code: str, basic_map: dict, value_map: dict, dividend_map: dict) -> dict:
    basic = basic_map.get(etf_code, {})
    value = value_map.get(etf_code, {})
    dividend = dividend_map.get(etf_code, {})

    value_fee = value.get("fee")
    mgmt_fee = value_fee if value_fee is not None else ((basic.get("managementFee") or 0) + (basic.get("custodyFee") or 0)) or None
    inception_raw = dividend.get("listingDate") or basic.get("listingDate")
    inception_date = inception_raw[:10] if inception_raw else None
    shares = basic.get("shares")
    book_value = value.get("bookValue")
    fund_size = None
    if shares and book_value:
        fund_size = round((shares * book_value) / 100_000_000, 2)
    if mgmt_fee is not None and (mgmt_fee < 0 or mgmt_fee > 5):
        mgmt_fee = None
    metadata = {
        "managementFee": round(mgmt_fee, 4) if mgmt_fee else None,
        "fundSize": fund_size,
        "issuer": _issuer(basic["manager"]) if basic.get("manager") else None,
        "inceptionDate": inception_date,
        "trailingYield": value.get("last4SeasonYR"),
        "beneficiaryCount": _latest_beneficiary_count(value),
    }

    metadata.update(fetch_dividend_frequency(etf_code))
    return metadata


def update_etf_json(etf_code: str, index_map: dict, basic_map: dict, value_map: dict, dividend_map: dict) -> bool:
    json_path = ETF_DATA_DIR / f"{etf_code}.json"
    if not json_path.exists():
        entry = index_map.get(etf_code)
        if not entry:
            print(f"  [WARN] {etf_code} 不在 index.json，跳過")
            return False
        ensure_skeleton(entry)

    metadata = build_metadata_for_code(etf_code, basic_map, value_map, dividend_map)
    if not any(value not in ("", None) for value in metadata.values()):
        print(f"  [WARN] {etf_code} 無可用 metadata")
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
        KEY_ORDER = [
            "code", "name", "assetClass", "categoryId", "trackingIndex",
            "managementFee", "dividendFrequency", "inceptionDate",
            "fundSize", "issuer", "trailingYield", "beneficiaryCount",
            "description", "references", "topHoldings", "holdingsHistory", "lastUpdated",
        ]
        ordered = {k: data[k] for k in KEY_ORDER if k in data}
        ordered.update({k: v for k, v in data.items() if k not in ordered})
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(ordered, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"  [OK] {etf_code} — metadata 更新")
    else:
        print(f"  [Skip] {etf_code} — 無變動")

    return True


def main() -> None:
    index = _load_stock_map_index()
    index_map = {e["code"]: e for e in index}
    codes = sys.argv[1:] if len(sys.argv) > 1 else load_all_codes()

    print("先抓取玩股網 ETF 基本資料總表...")
    basic_map, value_map, dividend_map = build_wantgoo_metadata_maps()

    print(f"準備同步 {len(codes)} 支 ETF 基本資料")

    success = 0
    failed = []
    for i, code in enumerate(codes):
        print(f"\n[{i+1}/{len(codes)}] {code}")
        if update_etf_json(code, index_map, basic_map, value_map, dividend_map):
            success += 1
        else:
            failed.append(code)

        if i < len(codes) - 1:
            time.sleep(0.3)

    print(f"\nETF 基本資料同步完成 — 成功: {success}/{len(codes)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")


if __name__ == "__main__":
    main()
