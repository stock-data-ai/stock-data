"""內部人持股餘額（董監事、經理人、大股東的持股與質押）。

來源是三支開放資料端點，各對應一個板別，**一次拿全市場**：
    上市 openapi.twse.com.tw/v1/opendata/t187ap11_L
    上櫃 www.tpex.org.tw/openapi/v1/mopsfin_t187ap11_O
    興櫃 www.tpex.org.tw/openapi/v1/mopsfin_t187ap11_R

每月一期、合計約 5.2 萬筆、涵蓋 2,334 家。端點只回最新一期、沒有查詢參數，
所以歷史靠每期累積（同 TDCC 股權分散的處境）。

> [!WARNING]
> **三個板別的欄位名互不相同，上市那份的 `選任時持股 ` 結尾還多一個空格。**
> 上市／上櫃：`公司代號`、`公司名稱`；興櫃：`SecuritiesCompanyCode`、`CompanyName`。
> 只比對其中一種寫法，會整批漏掉另外兩個板別。

> [!WARNING]
> **端點叫「董監事持股餘額」，內容其實是全體內部人。** 21 種職稱裡包含協理、
> 副總經理、總經理、大股東、會計／財務部門主管。所以合計要出兩組：
> `totals`（全體內部人）與 `boardTotals`（只算董監事本人）。
> **對外顯示用 boardTotals** —— 市場慣稱的「董監持股質押比」不含經理人與大股東。

授權：三支都在《政府資料開放授權條款》底下，逐筆明細可原樣呈現，需標示來源。
"""

import json
import logging
import time
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TIMEOUT_SEC = 120  # 上市那份約 10 MB
RETRIES = 3
RETRY_DELAY_SEC = 10

# > [!WARNING]
# > **一定要帶瀏覽器 User-Agent。** 2026-09-06 首次上 CI 就失敗：TPEx 對 GitHub runner
# > 的機房 IP ＋ 預設的 `Python-urllib` UA 直接回 `Connection reset by peer`
# > （本機跑同一支完全正常，所以測不出來）。repo 內其他 TWSE／TPEx 抓取一律帶 UA，
# > 這支當初漏了。
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

SOURCES = [
    ("twse", "https://openapi.twse.com.tw/v1/opendata/t187ap11_L"),
    ("tpex", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap11_O"),
    ("emerging", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap11_R"),
]

# 依序試，第一個非空的算數（見模組註解的欄位名警告）
FIELD_ALIASES = {
    "month": ("資料年月",),
    "code": ("公司代號", "SecuritiesCompanyCode"),
    "name": ("公司名稱", "CompanyName"),
    "role": ("職稱",),
    "person": ("姓名",),
    "elected": ("選任時持股 ", "選任時持股"),   # 上市那份有尾空格，順序不可對調
    "shares": ("目前持股",),
    "pledged": ("設質股數",),
}


def _pick(row: Dict[str, Any], key: str) -> str:
    for alias in FIELD_ALIASES[key]:
        v = row.get(alias)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _to_int(v: str) -> int:
    try:
        return int(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0


def roc_month_to_iso(v: str) -> Optional[str]:
    """民國 11507 → 2026-07。"""
    v = (v or "").strip()
    if len(v) != 5 or not v.isdigit():
        return None
    return f"{int(v[:3]) + 1911:04d}-{v[3:]}"


def is_board(role: str) -> bool:
    """是不是董監事本人。市場慣稱的「董監質押比」只算這一群。

    用包含比對而非白名單：職稱有 21 種且會變動，「獨立董事本人」「常務董事本人」
    都該算進來，協理／總經理／大股東不該。

    **法人代表人要排除**——「董事之法人代表人」的持股會與法人本身重複計算。
    2026-09-05 拿 1,080 家與官方 t187ap09_L 逐家對帳：含代表人只有 750 家（69%）
    對得上，排除後 851 家（79%）。中位數差 0.000pp，剩下的兩成多半是兩份資料的
    基準日不同。**要求逐家對得上官方數字的用途不適合用這個算法。**
    """
    return ("董事" in role or "監察人" in role) and "法人代表" not in role


def _merge_same_person(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把「同一人多職」併成一列。

    來源一列一個職稱，一人身兼數職就會出現多列（台積電魏哲家同時是董事長本人與
    總經理本人，持股數完全相同）。2026-09-05 實測 2,334 家裡 2,322 家中招（99%），
    多出 10,279 列、占全體 20%——不合併的話合計會把同一筆持股算兩次，人數也灌水。

    **只在持股數完全相同時才併**：實測只有 5 組同名而持股不同，那多半真的是不同人。
    """
    merged: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        key = (row["person"], row["shares"], row["pledged"])
        hit = merged.get(key)
        if hit:
            hit["roles"].extend(r for r in row["roles"] if r not in hit["roles"])
        else:
            merged[key] = row
    return list(merged.values())


def _summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = sum(r["shares"] for r in rows)
    pledged = sum(r["pledged"] for r in rows)
    return {
        "people": len(rows),
        "shares": total,
        "pledged": pledged,
        "pledgePct": round(pledged / total * 100, 2) if total > 0 else None,
    }


def _fetch(url: str) -> Optional[List[Dict[str, Any]]]:
    """抓一支端點，失敗重試。全部失敗回 None。

    機房 IP 打 TPEx 會間歇性被重置，重試一次多半就過——本機測不出這個症狀。
    """
    req = urllib.request.Request(url, headers=_HEADERS)
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as res:
                return json.loads(res.read().decode("utf-8"))
        except Exception as e:
            logger.warning(f"  {url} 第 {attempt + 1}/{RETRIES} 次失敗：{e}")
            if attempt < RETRIES - 1:
                time.sleep(RETRY_DELAY_SEC)
    return None


def fetch_all_insider_holdings() -> Optional[Dict[str, Dict[str, Any]]]:
    """抓三個板別並整併成 {公司代號: {month, insiders, totals, boardTotals}}。

    任一板別失敗就整批回 None——這是每期全量覆寫，缺一個板別會讓那批公司
    悄悄停在上一期，比整批不更新更難察覺。
    """
    by_company: Dict[str, Dict[str, Any]] = {}

    for market, url in SOURCES:
        rows = _fetch(url)
        if rows is None:
            logger.error(f"內部人持股 {market} 抓取失敗（已重試 {RETRIES} 次）")
            return None

        for r in rows:
            code = _pick(r, "code")
            if not code:
                continue
            entry = by_company.setdefault(code, {
                "month": roc_month_to_iso(_pick(r, "month")),
                "market": market,
                "insiders": [],
            })
            shares = _to_int(_pick(r, "shares"))
            pledged = _to_int(_pick(r, "pledged"))
            entry["insiders"].append({
                "roles": [_pick(r, "role")],
                "person": _pick(r, "person"),
                "elected": _to_int(_pick(r, "elected")),
                "shares": shares,
                "pledged": pledged,
                # 質押比自己算，不用原始的「設質股數佔持股比例」字串（帶 % 又要再 parse）。
                # 持股 0 時回 None——寫 0 會被讀成「沒質押」，語意完全不同。
                "pledgePct": round(pledged / shares * 100, 2) if shares > 0 else None,
            })
        logger.info(f"  內部人持股 {market}：{len(rows)} 筆")

    if not by_company:
        logger.error("內部人持股：三個板別都沒有資料")
        return None

    for entry in by_company.values():
        entry["insiders"] = _merge_same_person(entry["insiders"])
        # 持股多的排前面：一家最多 250 人，不排序的話畫面第一眼看到的是隨機的小股東
        entry["insiders"].sort(key=lambda i: -i["shares"])
        entry["totals"] = _summarize(entry["insiders"])
        entry["boardTotals"] = _summarize([i for i in entry["insiders"] if any(map(is_board, i["roles"]))])

    total_rows = sum(len(e["insiders"]) for e in by_company.values())
    months = {e["month"] for e in by_company.values() if e["month"]}
    logger.info(f"內部人持股：{len(by_company)} 家、{total_rows} 筆｜資料年月 {sorted(months)}")
    return by_company
