"""
【一次性】從 FinMind 補齊季配/半年配公司的每期股利明細（2021-2025）。

取代 CSV 年度合計，讓前端圖表能正確顯示每年完整加總。
安全：idempotent，可重複執行。
"""
import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TARGET_YEARS = set(range(2021, 2026))

# FinMind year 後綴 → (我們的 period, sequence)
_PERIOD_MAP = {
    "前半年度": ("上半年", 1),
    "後半年度": ("下半年", 2),
    "第1季": ("第1季", 1),
    "第2季": ("第2季", 2),
    "第3季": ("第3季", 3),
    "第4季": ("第4季", 4),
}

# period → dividendFrequency
_PERIOD_TO_FREQ = {
    "上半年": "半年", "下半年": "半年",
    "第1季": "季", "第2季": "季", "第3季": "季", "第4季": "季",
    "年度": "年",
}


def _parse_year_str(year_str: str) -> Optional[Tuple[int, str, int]]:
    """
    "110年第1季"    → (2021, "第1季", 1)
    "112年前半年度" → (2023, "上半年", 1)
    "110年"         → (2021, "年度", 1)
    Returns None if unparseable.
    """
    m = re.match(r'^(\d{2,3})年(.*)$', str(year_str or "").strip())
    if not m:
        return None
    gregorian = int(m.group(1)) + 1911
    suffix = m.group(2).strip()
    if not suffix:
        return (gregorian, "年度", 1)
    mapped = _PERIOD_MAP.get(suffix)
    if not mapped:
        return None
    return (gregorian, *mapped)


def _safe_float(val) -> float:
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


def run_backfill_finmind_dividends(args):
    """【一次性】從 FinMind 補齊季配/半年配公司每期股利明細（2021-2025）。"""
    from finance_tools.core.file_manager import FileManager
    from finance_tools.core.timezone import today_str

    try:
        from FinMind.data import DataLoader
    except ImportError:
        logger.error("FinMind 未安裝，請執行: pip install FinMind")
        return

    file_mgr = FileManager()
    dl = DataLoader()

    # 找出所有季配/半年配公司
    target_codes = []
    for fname in os.listdir(file_mgr.financials_dir):
        if not fname.endswith('.json'):
            continue
        code = fname.replace('.json', '')
        d = file_mgr.load_financial_data(code)
        if not d:
            continue
        freq = (d.get('latest') or {}).get('dividendFrequency', '')
        if freq in ('季', '半年'):
            target_codes.append(code)

    logger.info(f"目標：{len(target_codes)} 家季配/半年配公司")

    success, skip, failed = 0, 0, []

    for i, code in enumerate(sorted(target_codes), 1):
        try:
            # FinMind date 欄位是除息日，財年可能比除息日早一年
            # 用 2020-01-01 確保能抓到財年 2021 的全部期次
            df = dl.taiwan_stock_dividend(stock_id=code, start_date='2020-01-01')
            if df.empty:
                logger.debug(f"  [{code}] FinMind 無資料")
                skip += 1
                continue

            # 解析每一列
            finmind_records: Dict[int, List] = {}
            latest_period = None

            for _, row in df.iterrows():
                parsed = _parse_year_str(row.get('year', ''))
                if not parsed:
                    continue
                yr, period, seq = parsed
                if yr not in TARGET_YEARS:
                    continue

                cash = _safe_float(row.get('CashEarningsDistribution')) + _safe_float(row.get('CashStatutorySurplus'))
                stock = _safe_float(row.get('StockEarningsDistribution')) + _safe_float(row.get('StockStatutorySurplus'))
                if cash == 0 and stock == 0:
                    continue

                ex_date = str(row.get('CashExDividendTradingDate') or row.get('StockExDividendTradingDate') or '').strip() or None
                payout = str(row.get('CashDividendPaymentDate') or '').strip() or None

                finmind_records.setdefault(yr, []).append({
                    "year": yr,
                    "period": period,
                    "sequence": seq,
                    "cashDividend": round(cash, 4),
                    "stockDividend": round(stock, 4),
                    "totalDividend": round(cash + stock, 4),
                    "exDividendDate": ex_date,
                    "payoutDate": payout,
                })
                latest_period = period

            if not finmind_records:
                logger.debug(f"  [{code}] 無符合 2021-2025 的資料")
                skip += 1
                continue

            # 載入並更新
            financial_data = file_mgr.load_financial_data(code)
            if not financial_data:
                skip += 1
                continue

            existing = (financial_data.get('historical') or {}).get('dividends') or []
            finmind_years = set(finmind_records.keys())
            kept = [d for d in existing if d['year'] not in finmind_years]
            new_flat = [r for recs in finmind_records.values() for r in recs]

            financial_data.setdefault('historical', {})['dividends'] = sorted(
                new_flat + kept,
                key=lambda r: (r['year'], r.get('sequence', 1)),
                reverse=True,
            ) or None

            # 更新 dividendFrequency（以最新 period 推斷）
            if latest_period and latest_period in _PERIOD_TO_FREQ:
                financial_data.setdefault('latest', {})['dividendFrequency'] = _PERIOD_TO_FREQ[latest_period]

            today = today_str()
            financial_data['dividendsUpdated'] = today
            financial_data['lastUpdated'] = today

            if file_mgr.save_financial_data(code, financial_data):
                success += 1
            else:
                logger.error(f"  [{code}] save 失敗")
                failed.append(code)

            if i % 20 == 0:
                logger.info(f"  進度 {i}/{len(target_codes)}, 成功 {success}")

            time.sleep(0.3)  # FinMind rate limit

        except Exception as e:
            logger.error(f"  [{code}] 例外: {e}")
            failed.append(code)
            time.sleep(1)

    logger.info(f"\nFinMind backfill 完成：{success} 更新 / {skip} 跳過 / {len(failed)} 失敗")
    if failed:
        logger.error(f"失敗清單: {failed}")
