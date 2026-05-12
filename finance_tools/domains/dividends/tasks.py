import logging
from finance_tools.core.file_manager import FileManager
from finance_tools.core.timezone import today_str
from finance_tools.domains.dividends.csv_fetcher import load_all as load_csv
from finance_tools.domains.dividends.mops_fetcher import fetch_all as fetch_mops

logger = logging.getLogger(__name__)

# 各頻率需要的完整期次集合
_COMPLETE_PERIODS = {
    "年": {"年度"},
    "半年": {"上半年", "下半年"},
    "季": {"第1季", "第2季", "第3季", "第4季"},
}


def _mops_year_complete(year_records: list, freq: str) -> bool:
    """MOPS 某年的資料是否齊全（不齊 → 保留 CSV 合計）。"""
    expected = _COMPLETE_PERIODS.get(freq)
    if not expected:
        return True  # 未知頻率不判斷，直接用 MOPS
    present = {r["period"] for r in year_records}
    return expected.issubset(present)


def run_import_historical_dividends(args):
    """【一次性】從 CSV 匯入 2021–2025 歷史股利（年度合計，一年一筆）。
    直接以 CSV 涵蓋的公司為主，不依賴 topic index。
    """
    logger.info("Starting historical CSV dividend import (2021–2025)...")

    file_mgr = FileManager()

    logger.info("載入 CSV 歷史股利資料（2021–2025）...")
    csv_data, freq_map = load_csv()
    logger.info(f"CSV 涵蓋 {len(csv_data)} 家公司。")

    # 直接用 CSV 的 key 當作處理清單，不走 topic-based company loader
    all_codes = sorted(csv_data.keys())
    success_count = 0
    failed = []
    total = len(all_codes)

    for i, code in enumerate(all_codes, 1):
        name = code

        if i % 100 == 0:
            logger.info(f"  進度: {i}/{total}，已成功: {success_count}")

        records = csv_data.get(code, [])

        financial_data = file_mgr.load_financial_data(code)
        if not financial_data:
            if not records:
                continue
            financial_data = {
                "companyCode": code,
                "companyName": name,
                "latest": {},
                "historical": {"annual": [], "quarterly": [], "monthlyRevenue": [], "dividends": []},
            }
        financial_data.setdefault("historical", {})
        financial_data.setdefault("latest", {})

        csv_years = {r["year"] for r in records}
        existing = financial_data["historical"].get("dividends") or []
        kept = [d for d in existing if d["year"] not in csv_years]
        financial_data["historical"]["dividends"] = sorted(
            records + kept, key=lambda r: r["year"], reverse=True
        ) or None

        freq = freq_map.get(code)
        if freq:
            financial_data["latest"]["dividendFrequency"] = freq

        today = today_str()
        financial_data["dividendsUpdated"] = today
        financial_data["lastUpdated"] = today

        if file_mgr.save_financial_data(code, financial_data):
            success_count += 1
        else:
            logger.error(f"  X Failed to save {code}")
            failed.append(code)

    rerun_manager.save(failed)
    logger.info(f"OK CSV import: {success_count}/{total}")
    if failed:
        logger.error(f"X Failed ({len(failed)}): {failed[:20]}")


def run_update_mops_dividends(args):
    """【定期執行】從 MOPS 更新 2025+ 股利。
    各年度的 MOPS 資料若齊全（季配4期、半年配2期、年配1期），
    則用 MOPS 詳細記錄取代 CSV 合計；若不齊，保留 CSV 合計不動。
    """
    logger.info("Starting MOPS dividend update (2025+)...")

    file_mgr = FileManager()

    logger.info("從 MOPS 抓取最新股利公告...")
    mops_data, freq_map = fetch_mops()

    if not mops_data:
        logger.error("MOPS 無資料，中止。")
        return

    success_count = 0
    skip_count = 0
    incomplete_count = 0
    failed = []

    for code, records in mops_data.items():
        financial_data = file_mgr.load_financial_data(code)
        if not financial_data:
            skip_count += 1
            continue

        financial_data.setdefault("historical", {})
        financial_data.setdefault("latest", {})

        freq = (
            freq_map.get(code)
            or financial_data["latest"].get("dividendFrequency")
        )

        # 依年度分組 MOPS 記錄，各年獨立判斷
        from itertools import groupby
        existing = financial_data["historical"].get("dividends") or []
        existing_years = {d["year"] for d in existing}

        records_sorted = sorted(records, key=lambda r: r["year"])
        years_to_replace = set()
        for year, grp in groupby(records_sorted, key=lambda r: r["year"]):
            year_records = list(grp)
            if _mops_year_complete(year_records, freq):
                # 完整 → 直接用 MOPS
                years_to_replace.add(year)
            elif year not in existing_years:
                # 不完整，但 CSV 也沒有這年 → 還是寫入 MOPS（有比沒有好）
                years_to_replace.add(year)
            else:
                # 不完整且 CSV 有合計 → 保留 CSV
                incomplete_count += 1
                logger.debug(f"  [{code}] {year}年 MOPS 不完整（{freq}），保留 CSV 合計")

        if not years_to_replace:
            continue

        complete_records = [r for r in records if r["year"] in years_to_replace]
        kept = [d for d in existing if d["year"] not in years_to_replace]
        financial_data["historical"]["dividends"] = sorted(
            complete_records + kept,
            key=lambda r: (r["year"], r.get("sequence", 1)),
            reverse=True,
        ) or None

        if freq:
            financial_data["latest"]["dividendFrequency"] = freq

        today = today_str()
        financial_data["dividendsUpdated"] = today
        financial_data["lastUpdated"] = today

        if file_mgr.save_financial_data(code, financial_data):
            success_count += 1
        else:
            logger.error(f"  X Failed to save {code}")
            failed.append(code)

    logger.info(f"\n{'='*60}")
    logger.info(f"OK MOPS dividend update: {success_count} 更新, {skip_count} 跳過（無 JSON）, {incomplete_count} 年份保留 CSV")
    if failed:
        logger.error(f"X Failed ({len(failed)}): {failed[:20]}")
    logger.info(f"{'='*60}\n")
