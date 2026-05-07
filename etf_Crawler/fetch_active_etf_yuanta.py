# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests",
#   "beautifulsoup4",
# ]
# ///
"""
fetch_active_etf_yuanta.py

從元大投信官網 (yuantaetfs.com) 爬取主動型 ETF 每日持股明細，
更新 src/data/etf/{code}.json 的 topHoldings 與 holdingsHistory 欄位。

原理：
  頁面使用 Nuxt SSR，持股資料已嵌入 HTML 的 window.__NUXT__ 中，
  透過 Node.js subprocess 解析後取得 FundWeights.StockWeights 陣列，
  無需模擬「展開」按鈕操作。

支援 ETF：
  00990A  元大全球AI新經濟主動式ETF基金

用法：
    uv run etf_Crawler/fetch_active_etf_yuanta.py          # 更新全部
    uv run etf_Crawler/fetch_active_etf_yuanta.py 00990A   # 更新指定
"""

import json
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Optional

try:
    import requests
    from bs4 import BeautifulSoup
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("缺少依賴，請先執行：uv add requests beautifulsoup4")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"

BASE_URL = "https://www.yuantaetfs.com/product/detail/{code}/ratio"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

# 元大投信主動型 ETF 清單
YUANTA_ACTIVE_ETFS = [
    "00990A",  # 元大全球AI新經濟主動式ETF基金
]

# Node.js 腳本：解析 window.__NUXT__ 並輸出 JSON
_NODE_SCRIPT = r"""
const vm = require('vm');
const fs = require('fs');
const data = fs.readFileSync(process.argv[2], 'utf8');

const code = 'var window = {}; ' + data + '; module.exports = window.__NUXT__;';
const ctx = vm.createContext({ module: { exports: {} }, exports: {} });

try {
  new vm.Script(code).runInContext(ctx);
} catch (e) {
  process.stderr.write('eval error: ' + e.message + '\n');
  process.exit(1);
}

const nuxt = ctx.module.exports;
if (!nuxt || !nuxt.data) {
  process.stderr.write('no nuxt.data\n');
  process.exit(1);
}

// Find weightData in data array
let wd = null;
for (const item of nuxt.data) {
  if (item && item.weightData) { wd = item.weightData; break; }
}
if (!wd) {
  process.stderr.write('weightData not found\n');
  process.exit(1);
}

const trandate = (wd.PCF && wd.PCF.trandate) || '';
const sw = (wd.FundWeights && wd.FundWeights.StockWeights) || [];

process.stdout.write(JSON.stringify({ trandate, stocks: sw }));
"""


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


def _find_node() -> Optional[str]:
    """找可用的 node 執行檔。"""
    for candidate in ["node", "/opt/homebrew/bin/node", "/usr/local/bin/node"]:
        try:
            result = subprocess.run(
                [candidate, "--version"], capture_output=True, timeout=5
            )
            if result.returncode == 0:
                return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def _parse_nuxt_via_node(nuxt_js: str, node_bin: str) -> Optional[dict]:
    """用 Node.js 解析 window.__NUXT__ 字串，回傳 {trandate, stocks}。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False, encoding="utf-8"
    ) as f:
        f.write(_NODE_SCRIPT)
        script_path = f.name

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as df:
        df.write(nuxt_js)
        data_path = df.name

    try:
        result = subprocess.run(
            [node_bin, script_path, data_path],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
        )
        if result.returncode != 0:
            print(f"  [WARN] Node.js 解析錯誤: {result.stderr[:200]}")
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        print(f"  [WARN] Node.js 執行失敗: {e}")
        return None
    finally:
        Path(script_path).unlink(missing_ok=True)
        Path(data_path).unlink(missing_ok=True)


def _parse_trandate(trandate: str) -> Optional[str]:
    """將 '20260408' 轉為 '2026-04-08'。"""
    if len(trandate) == 8 and trandate.isdigit():
        return f"{trandate[:4]}-{trandate[4:6]}-{trandate[6:8]}"
    return None


def fetch_holdings(etf_code: str, node_bin: str) -> tuple:
    """
    回傳 (holdings, tran_date_str)
    holdings: [{"name": ..., "weight": ..., "code": ..., "shares": ...}, ...]
    tran_date_str: "2026-04-08" 或 None
    """
    url = BASE_URL.format(code=etf_code)
    print(f"  抓取 {url}")

    try:
        resp = session.get(url, headers=HEADERS, timeout=45)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # 找含有 window.__NUXT__ 的 <script> 標籤
        nuxt_js = None
        for script in soup.find_all("script"):
            text = script.string or ""
            if "window.__NUXT__" in text:
                nuxt_js = text.strip()
                break

        if not nuxt_js:
            print(f"  [WARN] 找不到 window.__NUXT__ 資料")
            return [], None

        parsed = _parse_nuxt_via_node(nuxt_js, node_bin)
        if not parsed:
            return [], None

        tran_date = _parse_trandate(parsed.get("trandate", ""))

        stocks = parsed.get("stocks", [])

        if not stocks:
            print(f"  [WARN] 無持股資料")
            return [], None

        holdings = []
        for s in stocks:
            code_raw = str(s.get("code", "")).strip()
            name = str(s.get("name", "")).strip()
            weight = s.get("weights")
            qty = s.get("qty")

            if weight is None:
                continue
            try:
                weight = round(float(weight), 2)
            except (ValueError, TypeError):
                continue
            if weight <= 0:
                continue

            entry: dict = {
                "name": name,
                "weight": weight,
                "shares": int(qty) if qty is not None else None,
            }

            # 台股代號：4-6 位純數字
            if code_raw.isdigit() and 4 <= len(code_raw) <= 6:
                entry["code"] = code_raw
            elif code_raw:
                entry["foreignCode"] = code_raw

            holdings.append(entry)

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
    if h.get("foreignCode"):
        clean["foreignCode"] = h["foreignCode"]
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
            _key = lambda x: x.get("code") or x.get("foreignCode") or x.get("name", "")
            if sorted([_clean_snapshot(h) for h in holdings], key=_key) == sorted([_clean_snapshot(h) for h in prev_holdings], key=_key):
                print(f"  [SKIP] {etf_code} 數據無變化（{current_date}），跳過寫入")
                return True

        if "holdingsHistory" not in data:
            data["holdingsHistory"] = {}

        # 舊資料補存歷史
        if prev_date and prev_holdings and prev_date not in data["holdingsHistory"]:
            data["holdingsHistory"][prev_date] = [_clean_snapshot(h) for h in prev_holdings]

        # 計算比較欄位
        prev_map = {h.get("code") or h.get("foreignCode") or h.get("name"): h for h in prev_holdings}
        for h in holdings:
            key = h.get("code") or h.get("foreignCode") or h.get("name")
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

        print(f"  [OK] {etf_code} — {len(holdings)} 筆持股，資料日期 {current_date}")
        return True
    except Exception as e:
        print(f"  [ERROR] 寫入失敗: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(YUANTA_ACTIVE_ETFS)
    unknown = [t for t in targets if t not in YUANTA_ACTIVE_ETFS]
    if unknown:
        print(f"[ERROR] 不支援的 ETF：{', '.join(unknown)}")
        print(f"支援清單：{', '.join(YUANTA_ACTIVE_ETFS)}")
        sys.exit(1)

    node_bin = _find_node()
    if not node_bin:
        print("[ERROR] 找不到 node，請先安裝 Node.js")
        sys.exit(1)
    print(f"使用 Node.js: {node_bin}")

    success, failed = 0, []

    for i, etf_code in enumerate(targets):
        print(f"\n[{i+1}/{len(targets)}] {etf_code}")

        holdings, tran_date = fetch_holdings(etf_code, node_bin)
        if holdings:
            if update_etf_json(etf_code, holdings, tran_date):
                success += 1
            else:
                failed.append(etf_code)
        else:
            failed.append(etf_code)

        if i < len(targets) - 1:
            time.sleep(1)

    print(f"\n元大投信主動 ETF 更新完成 — 成功: {success}/{len(targets)}")
    if failed:
        print(f"失敗: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
