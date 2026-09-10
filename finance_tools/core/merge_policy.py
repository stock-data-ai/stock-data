"""財報檔的合併規則與保留上限 —— **唯一的宣告處**。

為什麼要有這支：`company-financials/{code}.json` 由 11 支程式各自 load → 改 → save，
規則散在每個呼叫端，於是同一個欄位長出兩套互斥的政策
（`monthlyRevenue` 一邊是「合併、留 72」、另一邊是「整批取代、留 36」，
跑到後者就把五年歷史砍成 13 個月）。數字寫在這裡，改一次就到處生效。

保留上限的依據一律是**消費端真正要用到的最長視窗**，不是憑感覺：
  - 個股頁的融資融券／借券／三大法人卡片：`slice(0, 20)` → 20 個交易日
  - 技術分析圖開籌碼疊圖：最長 `6mo` → 約 126 個交易日  ← 目前的上界
  - `build_chip_history.py`：`KEEP_DAYS = 30`
  - AI 籌碼分析：只取最新一筆
取 180 是在上界之上留約 1.4 倍餘裕，同時把「無上限成長」關掉。
"""

from typing import Any, Callable, Dict, Iterable, List, Optional

# ── 保留上限（None = 刻意不設限）────────────────────────────────────────

#: 月營收保留幾筆。六年，個股頁的年度／月度表格都在這個範圍內。
MONTHLY_REVENUE_LIMIT = 72

#: 以日期為 key 的籌碼歷史保留幾個交易日。依據見模組 docstring。
DAILY_HISTORY_LIMIT = 180

#: 這些集合刻意不設上限——季報／年報／股利是低頻且有長期查詢價值，
#: 內部人歷史每月只有一筆合計（約 100 bytes）。
UNBOUNDED = ("quarterly", "annual", "dividends", "insiderHoldingsHistory")

#: `institutionalInvestors` 與 `shareholderDataHistory` **不在這裡收斂**——
#: 它們由 `scripts/archive_historical_data.py` 按整年**搬進** archive（不是刪除），
#: 主檔只留當年＋前一年。放在那邊而不是合併路徑，是因為那是資料搬遷：
#: 要寫封存檔、要保證搬走的每一筆都找得回來，而且必須按整年切才不會讓 git
#: 每天長出 2341 個新 blob。合併路徑只管「這一筆怎麼併」，不管「舊的搬去哪」。
INST_INVESTORS_LIMIT: Optional[int] = None


# ── 合併原語 ──────────────────────────────────────────────────────────

def merge_by_key(
    existing: Optional[Iterable[Dict]],
    incoming: Optional[Iterable[Dict]],
    key: Callable[[Dict], Any],
    limit: Optional[int] = None,
    preserve: Iterable[str] = (),
) -> List[Dict]:
    """以 `key` 合併兩份紀錄，新的取代同 key 的舊的，其餘保留。

    - `incoming` 為空 → **保留 `existing` 原樣**。抓取失敗與「這期真的沒資料」
      都長這樣，一律不得清空既有歷史。
    - `preserve`：新紀錄沒有、但同 key 舊紀錄有的欄位，補回新紀錄上。
      用於「這個欄位不歸我管」的情況（季度的 currentRatio／debtRatio 由
      資產負債表任務另外寫入，損益表重抓不得把它沖掉）。
    - 排序一律新→舊，由 `key` 的自然序決定。
    - `limit` 只在**有新資料時**套用；沒有新資料就原樣返回，
      避免「今天沒抓到」變成「順手砍掉一批舊的」。
    """
    existing = list(existing or [])
    incoming = list(incoming or [])
    if not incoming:
        return existing

    old_by_key = {key(r): r for r in existing}
    merged = []
    for record in incoming:
        row = dict(record)
        old = old_by_key.get(key(record), {})
        for field in preserve:
            if field not in row and field in old:
                row[field] = old[field]
        merged.append(row)

    new_keys = {key(r) for r in incoming}
    merged.extend(r for r in existing if key(r) not in new_keys)
    merged.sort(key=key, reverse=True)
    return merged[:limit] if limit else merged


def trim_date_map(records: Optional[Dict[str, Any]], limit: Optional[int]) -> Dict[str, Any]:
    """以日期字串為 key 的歷史，只留最近 `limit` 筆。`limit` 為 None 則不動。

    key 是 `YYYY-MM-DD`（字典序＝時間序），所以直接用字串排序即可。
    """
    records = records or {}
    if not limit or len(records) <= limit:
        return records
    keep = sorted(records, reverse=True)[:limit]
    return {d: records[d] for d in sorted(keep)}
