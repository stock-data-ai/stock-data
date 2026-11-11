"""月營收公布：觀測站彙總表（申報期間逐日更新）與證交所／櫃買開放資料（月中整批發布）。

觀測站 `mopsov.twse.com.tw/nas/t21/{sii|otc}/t21sc03_{民國年}_{月}_{0|1}.html`
是靜態檔，公司申報後會出現在表上；`_0` 是本國公司、`_1` 是外國（KY）公司。
沒有申報日欄位，公布日由每天比對前一次得出（見 tasks.py）。
金控、保險不在這張表（2026-11-11 實測：上市少 15 家，全是金融保險業），
月中核對時由開放資料補上。

開放資料 `t187ap05_L`／`mopsfin_t187ap05_O` 要等截止日過後才整批發布
（115 年 9 月營收是 10/17 出表），只拿來核對。

舊網域 robots.txt 全站禁止爬蟲；使用者 2026-11-11 同意每月 1～13 號每天兩班、每班 4 張表。
"""
import html
import logging
import re
import urllib.request
from typing import Dict, List, Optional

from finance_tools.utils import http_fetch
from finance_tools.utils.twse_url import bust

logger = logging.getLogger(__name__)

MOPS_URL = "https://mopsov.twse.com.tw/nas/t21/{market}/t21sc03_{roc}_{month}_{kind}.html"
OPEN_DATA_URLS = {
    "sii": "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
    "otc": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O",
}
MARKETS = ("sii", "otc")
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-data/1.0)"}

_ROW = re.compile(r"<tr align=right><td align=center>(.*?)</tr>", re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)   # 表內混用 <td> 與 <Td>
_INDUSTRY = re.compile(r"產業別：([^<]+)</th>")
_GENERATED = re.compile(r"出表日期：(\d+)/(\d+)/(\d+)(?:<!--(\d\d:\d\d:\d\d)-->)?")


class SourceError(Exception):
    """來源回傳不是預期的那份表（月份不對、格式變了、連線失敗）。"""


def _num(s: str) -> Optional[float]:
    s = html.unescape(s).replace(",", "").strip()
    if s in ("", "-", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _thousand_to_ntd(s: str) -> Optional[float]:
    v = _num(s)
    return None if v is None else round(v * 1000)


def _pct(s: str) -> Optional[float]:
    v = _num(s)
    return None if v is None else round(v, 2)


def parse_mops_table(page: str, year: int, month: int) -> Dict:
    """解析一張觀測站彙總表。回傳 {generatedAt, rows}；「查無資料」回傳空 rows。"""
    roc = year - 1911
    if f"{roc}年{month}月份" not in page:
        raise SourceError(f"表頭不是 {roc} 年 {month} 月")
    m = _GENERATED.search(page)
    generated = None
    if m:
        y, mo, d, t = m.groups()
        generated = f"{int(y) + 1911}-{int(mo):02d}-{int(d):02d}" + (f" {t}" if t else "")
    if "查無資料" in page:
        return {"generatedAt": generated, "rows": []}

    rows: List[Dict] = []
    # 產業別標題出現在每個產業表之前，逐段切開才知道每列屬於哪個產業
    parts = _INDUSTRY.split(page)
    for i in range(1, len(parts), 2):
        industry = parts[i].strip()
        for raw in _ROW.findall(parts[i + 1]):
            cells = [re.sub(r"<[^>]+>", "", c) for c in _CELL.findall("<td align=center>" + raw)]
            if len(cells) < 11:
                continue
            code = html.unescape(cells[0]).strip()
            if not re.fullmatch(r"[0-9A-Z]{4,6}", code):
                continue
            note = html.unescape(cells[10]).strip()
            rows.append({
                "code": code,
                "name": html.unescape(cells[1]).strip(),
                "industry": industry,
                "revenue": _thousand_to_ntd(cells[2]),
                "prevMonth": _thousand_to_ntd(cells[3]),
                "lastYear": _thousand_to_ntd(cells[4]),
                "mom": _pct(cells[5]),
                "yoy": _pct(cells[6]),
                "cumulative": _thousand_to_ntd(cells[7]),
                "cumLastYear": _thousand_to_ntd(cells[8]),
                "cumYoy": _pct(cells[9]),
                "note": None if note in ("", "-") else note,
            })
    if not rows:
        raise SourceError("表頭正確但解析不到任何公司，格式可能變了")
    return {"generatedAt": generated, "rows": rows}


def _get_text(url: str) -> str:
    req = urllib.request.Request(bust(url), headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise SourceError(f"{url} HTTP {resp.status}")
        return resp.read().decode("cp950", "replace")


def fetch_mops(year: int, month: int) -> Dict[str, Dict]:
    """抓上市、上櫃各兩張表（本國、KY）。任何一張失敗就整批失敗，不拿半份資料比對。"""
    out: Dict[str, Dict] = {}
    for market in MARKETS:
        rows: List[Dict] = []
        generated = None
        for kind, foreign in ((0, False), (1, True)):
            url = MOPS_URL.format(market=market, roc=year - 1911, month=month, kind=kind)
            try:
                table = parse_mops_table(_get_text(url), year, month)
            except SourceError as e:
                raise SourceError(f"{url}：{e}") from e
            except Exception as e:  # noqa: BLE001
                raise SourceError(f"{url}：{e}") from e
            for r in table["rows"]:
                r["market"] = market
                r["foreign"] = foreign
            rows.extend(table["rows"])
            generated = generated or table["generatedAt"]
            logger.info(f"觀測站 {market} {'KY' if foreign else '本國'}：{len(table['rows'])} 家（出表 {table['generatedAt']}）")
        out[market] = {"generatedAt": generated, "rows": rows}
    return out


def parse_open_data(records: List[Dict], market: str) -> Dict:
    """開放資料一筆一家，數字單位千元。回傳 {month: 'YYYY-MM', generatedAt, rows}。"""
    if not records:
        raise SourceError(f"{market} 開放資料是空的")
    ym = records[0].get("資料年月", "")
    gen = records[0].get("出表日期", "")
    if not re.fullmatch(r"\d{5}", ym):
        raise SourceError(f"{market} 開放資料年月格式不對：{ym!r}")
    month = f"{int(ym[:3]) + 1911}-{ym[3:]}"
    generated = f"{int(gen[:3]) + 1911}-{gen[3:5]}-{gen[5:7]}" if re.fullmatch(r"\d{7}", gen) else None
    rows = []
    for r in records:
        if r.get("資料年月") != ym:
            continue
        rows.append({
            "code": r["公司代號"].strip(),
            "name": r["公司名稱"].strip(),
            "industry": r.get("產業別", "").strip(),
            "market": market,
            "revenue": _thousand_to_ntd(r.get("營業收入-當月營收", "")),
            "prevMonth": _thousand_to_ntd(r.get("營業收入-上月營收", "")),
            "lastYear": _thousand_to_ntd(r.get("營業收入-去年當月營收", "")),
            "mom": _pct(r.get("營業收入-上月比較增減(%)", "")),
            "yoy": _pct(r.get("營業收入-去年同月增減(%)", "")),
            "cumulative": _thousand_to_ntd(r.get("累計營業收入-當月累計營收", "")),
            "cumLastYear": _thousand_to_ntd(r.get("累計營業收入-去年累計營收", "")),
            "cumYoy": _pct(r.get("累計營業收入-前期比較增減(%)", "")),
        })
    return {"month": month, "generatedAt": generated, "rows": rows}


def fetch_open_data() -> Dict[str, Dict]:
    out = {}
    for market, url in OPEN_DATA_URLS.items():
        try:
            records = http_fetch.get_json(bust(url), headers=_HEADERS, timeout=60)
        except Exception as e:  # noqa: BLE001
            raise SourceError(f"{url}：{e}") from e
        out[market] = parse_open_data(records, market)
        logger.info(f"開放資料 {market}：{out[market]['month']}，{len(out[market]['rows'])} 家")
    return out
