"""
刪掉不在名單上的財報檔：company-financials/{code}.json 有、companies-all.json 沒有的一律刪。

**為什麼要有這支**：名單（companies-all.json）由 stock_map 對政府上市／上櫃／興櫃三份
名單自動鏡像，下市會自動從名單消失；但 stock-data 這側從來沒有對應的清除，而 TDCC 對
已終止買賣的個股照樣發股權分散表，於是檔案持續更新、`lastUpdated` 永遠新鮮，
下游排行把它當活的（2026-09-09 優你康 4150 上大戶加碼股榜首即此因）。
2026-09-09 實測 2300 檔裡 16 檔已下市。

**不留墓碑。** 「不在名單上」本身就是下游要的訊號；要考古翻 stock_map 的 git。

護欄（誤刪的代價遠高於留著孤兒）：
  - 名單本身要過 `FileManager.load_companies()` 的下界檢查（台股 ≥ 2000 檔）
  - 單次刪除上限 max(20, 2%)，超過視為名單殘缺，整批中止
  - 只碰 4 碼數字檔名，company-financials-us／ETF 不在範圍內

用法：uv run finance_tools/cli.py prune-financials [--dry-run]
"""

import logging
import os

from finance_tools.core.file_manager import FileManager

logger = logging.getLogger(__name__)

PRUNE_MAX_RATIO = 0.02
PRUNE_MIN_ABS = 20


def run(dry_run: bool = False) -> dict:
    fm = FileManager()
    roster = {c["code"] for c in fm.load_companies()}  # 名單缺席／殘缺會在這裡拋出
    on_disk = fm.list_financial_codes()

    orphans = sorted(on_disk - roster)
    not_yet = sorted(roster - on_disk)

    limit = max(PRUNE_MIN_ABS, int(len(on_disk) * PRUNE_MAX_RATIO))
    if len(orphans) > limit:
        raise RuntimeError(
            f"[prune] {len(orphans)} 檔不在名單上，超過安全上限 {limit}"
            f"（{len(on_disk)} 檔的 {PRUNE_MAX_RATIO:.0%}）—— 疑似名單殘缺，本次不刪任何檔。"
        )

    for code in orphans:
        path = os.path.join(fm.financials_dir, f"{code}.json")
        if dry_run:
            print(f"[prune] (dry-run) 會刪 {code}")
        else:
            os.remove(path)
            print(f"[prune] 刪除 {code}（已不在 companies-all.json）")

    print(
        f"[prune] 名單 {len(roster)} 檔｜目錄 {len(on_disk)} 檔｜"
        f"{'預計' if dry_run else '已'}刪除 {len(orphans)} 檔｜名單有但尚未建檔 {len(not_yet)} 檔"
    )
    if not_yet:
        # 新上市會在下次任何 fetcher 跑到它時建檔，這裡只列出來讓執行摘要看得到
        print(f"[prune] 尚未建檔：{' '.join(not_yet[:30])}{' …' if len(not_yet) > 30 else ''}")

    return {"deleted": orphans, "not_yet": not_yet, "dry_run": dry_run}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run(dry_run="--dry-run" in sys.argv)
