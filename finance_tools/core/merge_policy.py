"""財報檔的合併規則 —— **唯一的宣告處**。

為什麼要有這支：`company-financials/{code}.json` 由 11 支程式各自 load → 改 → save，
規則散在每個呼叫端，於是同一個欄位長出兩套互斥的政策
（`monthlyRevenue` 一邊是「合併、留 72」、另一邊是「整批取代、留 36」，
跑到後者就把六年歷史砍成 13 個月）。規則寫在這裡，改一次就到處生效。

**保留政策分兩層，依資料頻率決定，不是憑感覺：**

| 類別 | 集合 | 規則 | 舊資料 |
|---|---|---|---|
| 期別型列表（低頻，一年最多 12 筆） | `quarterly` `annual` `dividends` `insiderHoldingsHistory` | 不設限 | — |
| 同上 | `monthlyRevenue` | `MONTHLY_REVENUE_LIMIT` = 72 筆 | 丟棄 |
| 日期型字典（高頻，一年約 245 筆） | `institutionalInvestors` `marginTrading` `securitiesLending` `shareholderDataHistory` | 主檔留當年＋前一年 | **搬進封存** |

為什麼月營收可以直接丟而日期型要封存：量級差兩個數量級。
2026-09-11 實測每檔平均 —— 月營收 **4 KB**（且 App 只看 36 個月，上限是需求的 2 倍）、
三大法人 **384 KB**。丟掉 4 KB 裡最舊的部分不值得為它蓋一套封存機制。

消費端真正用得到的最長視窗（決定「當年＋前一年」夠不夠）：
  - 個股頁的融資融券／借券／三大法人卡片：`slice(0, 20)` → 20 個交易日
  - 技術分析圖開籌碼疊圖：最長 `6mo` → 約 126 個交易日  ← 上界
  - `build_chip_history.py`：`KEEP_DAYS = 30`
  - AI 籌碼分析：只取最新一筆
最少保留 12 個月（約 245 個交易日）＝上界的 1.9 倍。
"""

from typing import Any, Callable, Dict, Iterable, List, Optional

# ── 保留上限（None = 刻意不設限）────────────────────────────────────────

#: 月營收保留幾筆。六年，個股頁的年度／月度表格都在這個範圍內。
MONTHLY_REVENUE_LIMIT = 72

#: 這些集合刻意不設上限——季報／年報／股利是低頻且有長期查詢價值，
#: 內部人歷史每月只有一筆合計（約 100 bytes）。
UNBOUNDED = ("quarterly", "annual", "dividends", "insiderHoldingsHistory")

#: **所有以日期為 key 的歷史**（三大法人、融資融券、借券、大戶）都不在這裡收斂——
#: 一律由 `scripts/archive_historical_data.py` 按整年**搬進** archive（不是刪除），
#: 主檔只留當年＋前一年。
#:
#: 為什麼不在合併路徑做：那是資料搬遷，要寫封存檔、要保證搬走的每一筆都找得回來，
#: 而且必須按整年切，否則封存檔天天變動、git 每天長出 2341 個新 blob。
#: 合併路徑只管「這一筆怎麼併」，不管「舊的搬去哪」。
#:
#: 曾經有第二套規則（`DAILY_HISTORY_LIMIT = 180`：寫入時按**筆數**刪掉融資融券／
#: 借券的舊紀錄）。那是錯的——同一類資料兩套規則、兩種單位（年 vs 筆數）、
#: 兩種下場（封存 vs 刪除）。2026-09-11 移除，全部統一走封存。


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
