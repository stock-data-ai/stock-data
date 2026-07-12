import sys
from datetime import timedelta
import logging
import json
from pathlib import Path

from finance_tools.core import DataProcessor, FileManager
from finance_tools.core.timezone import now_tw
from finance_tools.core.trading_day import is_tw_trading_day, parse_yyyymmdd
from finance_tools.domains.institutional_investors.twse_fetcher import TWSEInstitutionalFetcher
from finance_tools.domains.institutional_investors.twse_shareholding_fetcher import TWSEShareholdingFetcher
from finance_tools.domains.valuation.twse_valuation_fetcher import TWSEValuationFetcher
from finance_tools.orchestration.company_processor import CompanyProcessor
from finance_tools.domains.institutional_investors.calculator import InstRatioCalculator
from finance_tools.utils.company_list_loader import load_companies_for_processing
from finance_tools.utils.quality_report import save_quality_report
import finance_tools.config as config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parent.parent.parent
_SEEDS_FILE = Path(__file__).parent.parent / "assets/seeds/inst_ratio_seeds.json"
_COMPANIES_FILE = _REPO_ROOT / "src/data/layer3/companies/companies-all.json"


def _load_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"無法讀取 {path}: {e}")
        return {}


def run_update_marketcap_inst(args):
    """
    每日更新：市值/估值 + 三大法人（皆為 TWSE/TPEx 批次，全市場一次撈取）。
    找不到資料 = 今日無交易，不算失敗，不產生 rerun queue。
    """
    logger.info("正在執行每日更新（市值 + 三大法人）...")

    batch = args.batch.split("/")[0] if getattr(args, "batch", None) else None

    processor_data = DataProcessor()
    file_mgr = FileManager()

    # ── TWSE/TPEx 批次預撈（全市場一次，取代 per-stock API）───────────────
    raw_date = getattr(args, "date", None)
    if raw_date:
        today_str = raw_date.replace("-", "")
    else:
        today_str = now_tw().strftime("%Y%m%d")
    inst_fetcher = TWSEInstitutionalFetcher()
    try:
        pre_inst = inst_fetcher.fetch_all(today_str)
    except Exception as e:
        logger.error(f"❌ TWSE T86 批次撈取失敗（含重試）: {e}，中止執行。")
        sys.exit(1)
    pre_shareholding = TWSEShareholdingFetcher().fetch_all()
    pre_valuation = TWSEValuationFetcher().fetch_all()

    if not pre_inst:
        logger.error("❌ TWSE/TPEx 三大法人批次撈取失敗（含重試），中止執行。")
        sys.exit(1)

    # 交易日卻拿不到當日核心資料 → STALE 失敗，不可綠燈（休市日拿不到是正常，照舊跳過）
    if is_tw_trading_day(parse_yyyymmdd(today_str)):
        if not inst_fetcher.listed_ok:
            logger.error(f"STALE: {today_str} 為交易日，但 TWSE T86 上市三大法人尚未公布或撈取失敗，終止任務。")
            sys.exit(1)
        if not pre_valuation:
            logger.error(f"STALE: {today_str} 為交易日，但市值估值資料撈取為空，終止任務。")
            sys.exit(1)

    logger.info(f"✅ TWSE/TPEx 批次資料就緒: inst={len(pre_inst)}, shareholding={len(pre_shareholding)}, valuation={len(pre_valuation)}")

    seeds = _load_json(_SEEDS_FILE)
    companies_data = _load_json(_COMPANIES_FILE)
    inst_ratio_calc = InstRatioCalculator(seeds=seeds, companies_data=companies_data)

    companies = load_companies_for_processing(args, file_mgr)
    if not companies:
        logger.info("沒有待處理的公司，退出。")
        return

    is_force_update = args.force or args.code is not None
    logger.info(f"正在處理 {len(companies)} 家公司（force={is_force_update}）。")

    company_processor = CompanyProcessor(
        processor=processor_data,
        file_mgr=file_mgr,
        finmind_client=None,
        financials_fetcher=None,
        revenue_fetcher=None,
        all_companies_details=companies_data,
        institutional_investors_shares_fetcher=None,
        shareholding_fetcher=None,
        inst_ratio_calculator=inst_ratio_calc,
    )

    start_date = (now_tw() - timedelta(days=config.DEFAULT_FETCH_DAYS)).strftime("%Y-%m-%d")

    processed = 0
    no_marketcap = []

    for idx, company in enumerate(companies, 1):
        code = company["code"]
        name = company.get("name", code)

        try:
            _, status = company_processor.process_daily_only(
                code=code,
                name=name,
                start_date=start_date,
                force_update=is_force_update,
                pre_inst=pre_inst,
                pre_shareholding=pre_shareholding,
                pre_valuation=pre_valuation,
            )

            if not status.get("skipped"):
                processed += 1
                if not status.get("marketcap"):
                    no_marketcap.append(f"{code} {name}")

            logger.info(f"[{idx}/{len(companies)}] ✔ {code} {name}"
                        + (" [無市值]" if not status.get("skipped") and not status.get("marketcap") else ""))

        except Exception:
            logger.exception(f"  ❌ 處理公司 {code} 時發生未預期錯誤:")

    save_quality_report("daily", batch, no_marketcap)

    logger.info(f"\n{'='*60}")
    logger.info(f"每日更新完成: {processed}/{len(companies)} 家公司")
    if no_marketcap:
        logger.info(f"今日無市值資料: {len(no_marketcap)} 家（記錄於 quality report）")
    logger.info(f"{'='*60}\n")
