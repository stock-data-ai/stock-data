"""
update_etf_basics.py

ETF 基本資料（雲端版，週六「ETF 週更」）：只用官方開放資料＋自己的配息紀錄，更新 src/data/etf/{code}.json：

  inceptionDate     上市日期        證交所 OpenAPI 基金基本資料彙總表（t187ap47_L，上市 ETF）
  beneficiaryCount  受益人數        集保開放資料「集保戶股權分散表」合計人數（上市、上櫃都有）
  fundSize          規模（億元）    發行單位數 × 最近收盤價。單位數優先用證交所的發行單位數，沒有（上櫃）用集保合計股數；
                                   收盤價用證交所／櫃買 OpenAPI 盤後收盤。**是市值估算，不是淨值規模**，與投信公布的淨資產略有差距
  trailingYield     近一年殖利率    最近一年份的配息 ÷ 最近收盤 × 100；上市未滿一年不算（配息讀 src/data/etf/dividends，FinMind 更新）
  dividendFrequency 配息頻率        由最近幾次除息的間隔推算；上市滿 2 年、近 3 年沒配過標「不配息」；配息不到 3 次的新 ETF 不動

不更新：管理費（沒有公開 API，人工維護：本機排程每月提醒整理 CSV，用 compare_etf_fees.py 對照）、
發行商、名稱、分類（靜態，已存）。新 ETF：stock_map index.json 加條目後，--skeletons-only 建骨架檔（排程在抓持股前先跑）。

2026-11-04 起取代本機 Playwright 爬玩股網／MoneyDJ 的 sync_etf_metadata.py。

用法：
    uv run python etf_Crawler/update_etf_basics.py            # 全部
    uv run python etf_Crawler/update_etf_basics.py 0056 00981A
    uv run python etf_Crawler/update_etf_basics.py --dry-run  # 只印差異，不寫檔
    uv run python etf_Crawler/update_etf_basics.py --skeletons-only  # 只替 index.json 有、detail 沒有的新 ETF 建骨架
"""

import csv
import io
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests

REPO_ROOT = Path(__file__).parent.parent
ETF_DIR = REPO_ROOT / "src/data/etf"
DIV_DIR = ETF_DIR / "dividends"

TWSE_FUNDS_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap47_L"
TWSE_CLOSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_CLOSE_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
# 集保開放資料兩個網域都提供同一份（約 2.4 MB）；opendata 約 3 秒、smart 約 20～60 秒且偶爾中途斷線，
# 先試快的，失敗再換另一個
TDCC_URLS = ["https://opendata.tdcc.com.tw/getOD.ashx?id=1-5", "https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5"]

FIELDS = ["inceptionDate", "beneficiaryCount", "fundSize", "trailingYield", "dividendFrequency"]
PAYMENTS_PER_YEAR = {"月配": 12, "雙月配": 6, "季配": 4, "半年配": 2, "年配": 1}
# compute() 回這個＝明確清成 null（None 是「這次算不出來，不動原值」）
CLEAR = "__clear__"


def _get(url: str, timeout: int = 120, retries: int = 5):
    """開放資料偶爾中途斷線（11/4 實測證交所／櫃買收盤與集保都出現過 Response ended prematurely）：重試幾次。"""
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=timeout)
            resp.raise_for_status()
            _ = resp.content  # 讀完整個內容，斷線在這裡就會丟例外
            return resp
        except requests.RequestException as err:
            last = err
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"{url} 重試 {retries} 次仍失敗：{last}")


def roc_to_iso(roc: str) -> Optional[str]:
    """1150409 → 2026-04-09"""
    s = (roc or "").strip()
    if len(s) != 7 or not s.isdigit():
        return None
    return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:]}"


def _num(x) -> Optional[float]:
    try:
        v = float(str(x).replace(",", "").strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def load_twse_funds() -> Dict[str, Dict]:
    rows = _get(TWSE_FUNDS_URL).json()
    return {r["基金代號"].strip(): {"listed": roc_to_iso(r.get("上市日期")), "units": _num(r.get("發行單位數/轉換數"))} for r in rows}


def load_closes() -> Dict[str, Dict]:
    """代號 → {close, date}；上市、上櫃合併（同代號以上市為準）。"""
    out: Dict[str, Dict] = {}
    for r in _get(TPEX_CLOSE_URL).json():
        c = _num(r.get("Close"))
        if c:
            out[r["SecuritiesCompanyCode"].strip()] = {"close": c, "date": roc_to_iso(r.get("Date"))}
    for r in _get(TWSE_CLOSE_URL).json():
        c = _num(r.get("ClosingPrice"))
        if c:
            out[r["Code"].strip()] = {"close": c, "date": roc_to_iso(r.get("Date"))}
    return out


def load_tdcc() -> Dict[str, Dict]:
    """集保股權分散表：代號 → {holders, units, date}，取持股分級 17（合計）。"""
    text = None
    for url in TDCC_URLS:
        try:
            text = _get(url, timeout=120).content.decode("utf-8-sig")
            break
        except Exception as err:
            print(f"  [WARN] 集保 {url} 失敗：{err}")
    if text is None:
        raise RuntimeError("集保股權分散表兩個網域都抓不到")
    out: Dict[str, Dict] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 5 or row[2].strip() != "17":
            continue
        out[row[1].strip()] = {"holders": int(row[3]) if row[3].strip().isdigit() else None,
                               "units": _num(row[4]), "date": row[0].strip()}
    return out


def recent_dividends(code: str, today: date, days: int) -> List[Dict]:
    path = DIV_DIR / f"{code}.json"
    if not path.exists():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    since = (today - timedelta(days=days)).isoformat()
    rows = [d for d in doc.get("dividends") or [] if (d.get("date") or "") > since]
    return sorted(rows, key=lambda d: d.get("date") or "", reverse=True)  # 新到舊


def infer_frequency(dates: List[str]) -> Optional[str]:
    """
    最近幾次除息日（新到舊）→ 配息頻率（同 MoneyDJ 用詞）。看相鄰兩次間隔天數的中位數，
    不數「一段期間內配了幾次」：區間邊界常多數一次，季配會被數成 5 次（11/4 實測 27 檔判成雙月配）。
    至少要有 3 次（2 個間隔）才判斷，太新的 ETF 不猜。
    """
    ds = [date.fromisoformat(d) for d in dates[:6]]
    gaps = sorted((a - b).days for a, b in zip(ds, ds[1:]))
    if len(gaps) < 2:
        return None
    mid = gaps[len(gaps) // 2] if len(gaps) % 2 else (gaps[len(gaps) // 2 - 1] + gaps[len(gaps) // 2]) / 2
    for limit, label in ((45, "月配"), (75, "雙月配"), (120, "季配"), (240, "半年配"), (450, "年配")):
        if mid <= limit:
            return label
    return None


def compute(code: str, today: date, funds: Dict, closes: Dict, tdcc: Dict, current: Dict) -> Dict:
    """回傳要寫入的欄位（None＝這次算不出來，不動原值）。"""
    f = funds.get(code, {})
    t = tdcc.get(code, {})
    px = closes.get(code, {}).get("close")
    units = f.get("units") or t.get("units")
    listed = f.get("listed") or current.get("inceptionDate")
    year_old = bool(listed) and listed <= (today - timedelta(days=400)).isoformat()

    out: Dict = {k: None for k in FIELDS}
    out["inceptionDate"] = f.get("listed")
    out["beneficiaryCount"] = t.get("holders")
    out["fundSize"] = round(units * px / 1e8, 2) if units and px else None
    recent = [d for d in recent_dividends(code, today, 3 * 365) if d.get("date")]
    paid = [d["date"] for d in recent]
    out["dividendFrequency"] = infer_frequency(paid)
    # 近一年殖利率：近 365 天的配息加總 ÷ 收盤；次數比頻率該有的少（年配 1、半年配 2、季配 4、雙月配 6、月配 12）
    # 就往前補到 N 次——年配的除息日每年差幾天，只看 365 天會在 0 與正常值之間跳（11/4 實測 00739 變 0）。
    # 最近一次超過 13 個月＝這一年沒配。只算已除息的，不含已公告未除息。
    freq = out["dividendFrequency"] or current.get("dividendFrequency")
    per_year = PAYMENTS_PER_YEAR.get(freq or "")
    # 上市未滿一年沒有完整一年的配息，「近一年殖利率」不算（11/4 實測 00991B 上市 3 個月只配一次，算出 0.44% 誤導）
    one_year = bool(listed) and listed <= (today - timedelta(days=365)).isoformat()
    if not one_year:
        out["trailingYield"] = CLEAR
    elif px and recent and recent[0]["date"] > (today - timedelta(days=400)).isoformat():
        within = [d for d in recent if d["date"] > (today - timedelta(days=365)).isoformat()]
        # 近 365 天的全算（配息變頻，例如 00770 今年配兩次也算到）；比頻率少時往前補到 N 次（年配剛過一年幾天）
        take = within if not per_year or len(within) >= per_year else recent[:per_year]
        out["trailingYield"] = round(sum(d.get("cashDividend") or 0 for d in take) / px * 100, 2)
    elif px and year_old:
        out["trailingYield"] = 0.0
    # 上市滿 2 年、近 3 年一次都沒配、原本也是空白：補「不配息」（槓桿／反向／期貨型多半如此；前端排序認得）。
    # 原本有值（MoneyDJ 的配息政策，例如「年配」但多年沒配）不覆蓋：政策與實際沒配是兩回事
    if not paid and not current.get("dividendFrequency") and listed and listed <= (today - timedelta(days=730)).isoformat():
        out["dividendFrequency"] = "不配息"
    return out


def ensure_skeletons(dry: bool = False) -> List[str]:
    """
    stock_map index.json 有、detail 檔還沒有的 ETF：建骨架檔（人工欄位取自 index）。
    持股爬蟲（CMoney 週更、主動式每日）找不到 detail 檔會直接跳過，所以新 ETF 上架靠這一步；
    原本在已刪除的 sync_etf_metadata.py（ensure_skeleton），2026-11-04 移到這裡。
    雲端排程先把 stock_map 的 index.json 抓到 src/data/etf/index.json 再跑。
    """
    index_path = ETF_DIR / "index.json"
    if not index_path.exists():
        return []
    created = []
    for entry in json.loads(index_path.read_text(encoding="utf-8")):
        code = str(entry.get("code") or "").strip()
        path = ETF_DIR / f"{code}.json"
        if not code or path.exists():
            continue
        skeleton = {k: entry.get(k) for k in ("code", "name", "assetClass", "categoryId", "trackingIndex", "managementFee",
                                               "dividendFrequency", "inceptionDate", "fundSize", "issuer", "trailingYield", "beneficiaryCount")}
        skeleton.update({"code": code, "name": entry.get("name") or code, "description": None,
                         "topHoldings": [], "holdingsHistory": {}, "lastUpdated": None})
        if not dry:
            path.write_text(json.dumps(skeleton, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        created.append(code)
    return created


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    if "--skeletons-only" in sys.argv:
        created = ensure_skeletons(dry)
        print(f"ETF 骨架：新建 {len(created)} 檔{('：' + ', '.join(created)) if created else ''}")
        return
    today = date.today()
    codes = args or sorted(p.stem for p in ETF_DIR.glob("*.json") if p.stem != "index")

    try:
        funds, closes, tdcc = load_twse_funds(), load_closes(), load_tdcc()
    except Exception as err:
        print(f"::error::ETF 基本資料來源抓取失敗：{err}")
        sys.exit(1)
    print(f"來源：證交所基金 {len(funds)} 檔、收盤 {len(closes)} 檔、集保 {len(tdcc)} 檔")

    changed, missing = 0, []
    for code in codes:
        path = ETF_DIR / f"{code}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        new = compute(code, today, funds, closes, tdcc, data)
        if new.get("beneficiaryCount") is None and new.get("fundSize") is None:
            missing.append(code)
        new = {k: (None if v == CLEAR else v) for k, v in new.items() if v is not None}
        diff = {k: (data.get(k), v) for k, v in new.items() if data.get(k) != v}
        if not diff:
            continue
        changed += 1
        if dry:
            print(code, json.dumps(diff, ensure_ascii=False))
            continue
        for k, (_, v) in diff.items():
            data[k] = v
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"ETF 基本資料更新完成 — {changed} 檔有變動，{len(codes)} 檔檢查，{len(missing)} 檔查無受益人數與規模")
    # 證交所有、網站還沒上架的 ETF：上架是人工（stock_map index 建條目 → 建 detail 骨架），
    # 不列出來很容易漏（11/4 盤點有 50 檔，含新的主動式 00416A、00415A）。只提醒，不自動建
    site_codes = {p.stem for p in ETF_DIR.glob("*.json") if p.stem != "index"}
    # 只列最近一天有收盤的（還在交易）：證交所基金表也含已下市的老代號（0054、0058…）
    # 加掛外幣的 K 股（00636K 等）是同一檔基金的外幣版本，網站已有新台幣版，不收（2026-11-04 使用者決定）
    new_etfs = sorted(c for c in funds if c not in site_codes and c in closes and not c.endswith("K"))
    print(f"NEW_ETF 證交所上市、還在交易但網站沒有 {len(new_etfs)} 檔：{', '.join(new_etfs[:40])}{' …' if len(new_etfs) > 40 else ''}")
    if missing:
        print(f"查無：{', '.join(missing)}")
    # 大量查無＝來源格式變了或抓錯日子：紅燈
    if codes and len(missing) / len(codes) > 0.2:
        print(f"::error::{len(missing)} 檔查無受益人數與規模，超過 20%")
        sys.exit(1)


if __name__ == "__main__":
    main()
