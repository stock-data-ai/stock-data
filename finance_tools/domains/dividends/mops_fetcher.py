"""
MOPS Dividend Fetcher - 從公開資訊觀測站開放資料抓取最新股利公告（2025+）
每筆配息獨立儲存，支援季/半年/年配。
"""

import logging
import pandas as pd
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

_URLS = {
    "L": "https://mopsfin.twse.com.tw/opendata/t187ap45_L.csv",  # 上市
    "O": "https://mopsfin.twse.com.tw/opendata/t187ap45_O.csv",  # 上櫃
}

_PERIOD_TO_FREQ = {
    "年度": "年",
    "上半年": "半年", "下半年": "半年",
    "第1季": "季", "第2季": "季", "第3季": "季", "第4季": "季",
}

# 從 period 推算季度編號（不依賴 MOPS 的「期別」欄位，後者代表公告順序）
_PERIOD_TO_SEQUENCE = {
    "年度": 1,
    "上半年": 1, "下半年": 2,
    "第1季": 1, "第2季": 2, "第3季": 3, "第4季": 4,
}


def _roc_to_date(val) -> Optional[str]:
    """民國 7 碼整數 (e.g. 1150210) → 'YYYY-MM-DD'，失敗回 None"""
    try:
        s = str(int(val)).zfill(7)
        y = int(s[:3]) + 1911
        return f"{y}-{s[3:5]}-{s[5:7]}"
    except Exception:
        return None


def _roc_range_to_date(raw: str) -> Optional[str]:
    """'1141001~1141231' → '2025-10-01~2025-12-31'"""
    raw = str(raw or "").strip()
    if "~" not in raw or len(raw) < 15:
        return None
    try:
        a, b = raw.split("~")
        def fmt(s):
            s = s.strip().zfill(7)
            return f"{int(s[:3])+1911}-{s[3:5]}-{s[5:7]}"
        return f"{fmt(a)}~{fmt(b)}"
    except Exception:
        return None


def _safe_float(val) -> float:
    try:
        if pd.isna(val):
            return 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def fetch_all() -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, str]]:
    """
    從 MOPS 抓取所有股利公告，回傳 (records_by_code, freq_by_code)。

    records_by_code: {code: [records sorted by (year asc, sequence asc)]}
    freq_by_code:    {code: "季"/"半年"/"年"}  ← 最新期的配息頻率
    """
    dfs = []
    for cat, url in _URLS.items():
        logger.info(f"Fetching {cat} from MOPS...")
        try:
            df = pd.read_csv(url, encoding="utf-8-sig")
            dfs.append(df)
        except Exception as e:
            logger.error(f"Failed to fetch {cat} from {url}: {e}")

    if not dfs:
        return {}, {}

    full = pd.concat(dfs, ignore_index=True)

    by_code: Dict[str, List[Dict[str, Any]]] = {}

    for _, row in full.iterrows():
        code = str(row.get("公司代號", "")).strip()
        if not code or len(code) < 4:
            continue

        try:
            mops_year = int(row.get("股利年度", 0))
            if mops_year == 0:
                continue
            fiscal_year = mops_year + 1911
        except Exception:
            continue

        cash = (
            _safe_float(row.get("股東配發-盈餘分配之現金股利(元/股)"))
            + _safe_float(row.get("股東配發-法定盈餘公積發放之現金(元/股)"))
            + _safe_float(row.get("股東配發-資本公積發放之現金(元/股)"))
        )
        stock = (
            _safe_float(row.get("股東配發-盈餘轉增資配股(元/股)"))
            + _safe_float(row.get("股東配發-法定盈餘公積轉增資配股(元/股)"))
            + _safe_float(row.get("股東配發-資本公積轉增資配股(元/股)"))
        )

        if cash == 0 and stock == 0:
            continue

        period = str(row.get("股利所屬年(季)度", "") or "").strip() or "年度"
        sequence = _PERIOD_TO_SEQUENCE.get(period, 1)

        by_code.setdefault(code, []).append({
            "year": fiscal_year,
            "period": period or "年度",
            "sequence": sequence,
            "cashDividend": round(cash, 4),
            "stockDividend": round(stock, 4),
            "totalDividend": round(cash + stock, 4),
            "payoutDate": _roc_to_date(row.get("董事會（擬議）股利分派日")),
            "periodRange": _roc_range_to_date(row.get("股利所屬期間")),
            "status": str(row.get("決議（擬議）進度", "") or "").strip() or None,
        })

    # sort by (year, sequence), infer frequency from latest period
    freq_by_code: Dict[str, str] = {}
    for code, records in by_code.items():
        records.sort(key=lambda r: (r["year"], r["sequence"]))
        latest_period = records[-1]["period"]
        freq_by_code[code] = _PERIOD_TO_FREQ.get(latest_period, "年")

    logger.info(f"MOPS: 處理 {len(by_code)} 家公司。")
    return by_code, freq_by_code
