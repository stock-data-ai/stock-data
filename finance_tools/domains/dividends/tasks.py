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
    """【定期執行】從 MOPS 更新股利。MOPS 有什麼就寫什麼，不再依賴 CSV。"""
    logger.info("Starting MOPS dividend update...")

    file_mgr = FileManager()

    logger.info("從 MOPS 抓取最新股利公告...")
    mops_data, freq_map = fetch_mops()

    if not mops_data:
        logger.error("MOPS 無資料，中止。")
        return

    success_count = 0
    skip_count = 0
    failed = []

    for code, records in mops_data.items():
        financial_data = file_mgr.load_financial_data(code)
        if not financial_data:
            skip_count += 1
            continue

        financial_data.setdefault("historical", {})
        financial_data.setdefault("latest", {})

        freq = freq_map.get(code) or financial_data["latest"].get("dividendFrequency")

        # MOPS 只含最近公告，用 (year, sequence) 粒度 upsert。
        # 不主動移除年度合計，讓前端 chart 過濾不完整年份。
        mops_keys = {(r["year"], r["sequence"]) for r in records}
        existing = financial_data["historical"].get("dividends") or []
        kept = [d for d in existing if (d["year"], d.get("sequence", 1)) not in mops_keys]
        financial_data["historical"]["dividends"] = sorted(
            records + kept,
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
    logger.info(f"OK MOPS dividend update: {success_count} 更新, {skip_count} 跳過（無 JSON）")
    if failed:
        logger.error(f"X Failed ({len(failed)}): {failed[:20]}")
    logger.info(f"{'='*60}\n")
