import json
import logging
from typing import List, Dict
from finance_tools.core.file_manager import FileManager
import finance_tools.config as config

logger = logging.getLogger(__name__)


def filter_already_updated(
    companies: List[Dict[str, str]],
    file_mgr: FileManager,
    force_update: bool = False,
) -> List[Dict[str, str]]:
    """
    過濾掉今日已更新的公司，避免在 loop 內對 skip 的公司做無意義的 sleep。
    force_update=True 時直接回傳原清單（--force / --code / --rerun 模式）。
    """
    if force_update:
        return companies
    before = len(companies)
    remaining = [c for c in companies if not file_mgr.is_updated_today(c["code"])]
    skipped = before - len(remaining)
    if skipped > 0:
        logger.info(f"跳過 {skipped} 家已於今日更新的公司，剩餘 {len(remaining)} 家需要更新。")
    return remaining


def load_companies_for_processing(args: object, file_mgr: FileManager, rerun_manager=None) -> List[Dict[str, str]]:
    """
    Loads a list of companies to process based on command-line arguments.
    Handles single code, topic, rerun, limit, and full run modes.

    Args:
        args: The parsed arguments object from argparse.
        file_mgr: An instance of the FileManager.
        rerun_manager: Optional RerunManager instance for --rerun mode.

    Returns:
        A list of company dictionaries, e.g., [{"code": "2330", "name": "台積電"}].
    """
    companies = []

    # Memoize the full company list to avoid redundant loads
    all_companies_map = None

    def get_all_companies_map():
        nonlocal all_companies_map
        if all_companies_map is None:
            all_companies = file_mgr.load_companies()
            all_companies_map = {c["code"]: c["name"] for c in all_companies}
        return all_companies_map

    if getattr(args, "code", None):
        logger.info(f"--- 單一公司模式: {args.code} ---")
        company_map = get_all_companies_map()
        if args.code in company_map:
            companies = [{"code": args.code, "name": company_map[args.code]}]
        else:
            logger.warning(f"找不到公司 {args.code}。")
            return []

    elif getattr(args, "topic", None):
        logger.info(f"--- 主題模式: {args.topic} ---")
        try:
            with open(str(config.COMPANY_TOPICS_INDEX_FILE), "r", encoding="utf-8") as f:
                topic_index = json.load(f)

            topic_codes = {code for code, topics in topic_index.items() if args.topic in topics}

            if not topic_codes:
                logger.warning(f"找不到主題 '{args.topic}' 的任何公司。")
                return []

            company_map = get_all_companies_map()
            companies = [{"code": code, "name": company_map.get(code, code)} for code in sorted(list(topic_codes))]
            logger.info(f"找到 {len(companies)} 家公司屬於主題 '{args.topic}'。")

        except FileNotFoundError:
            logger.error(f"找不到主題索引檔: {config.COMPANY_TOPICS_INDEX_FILE}")
            return []

    elif getattr(args, "rerun", None):
        logger.info("--- 重新執行模式 ---")
        if rerun_manager is None:
            logger.error("重新執行模式需要 rerun_manager。")
            return []
        codes = rerun_manager.load()
        if not codes:
            return []

        company_map = get_all_companies_map()
        companies = [{"code": code, "name": company_map.get(code, code)} for code in codes]
        logger.info(f"從重新執行佇列 ({rerun_manager.file_path}) 載入了 {len(companies)} 家公司。")

    else:
        logger.info("--- 完整執行模式 ---")
        all_companies_list = file_mgr.load_companies()
        companies = [{"code": c["code"], "name": c["name"]} for c in all_companies_list]

    # 過濾非四位數字的公司代碼（跳過美股、日股等）
    before_filter = len(companies)
    companies = [c for c in companies if c["code"].isdigit() and len(c["code"]) == 4]
    skipped = before_filter - len(companies)
    if skipped > 0:
        logger.info(f"已跳過 {skipped} 家非台灣公司 (非四位數代碼)。")

    if getattr(args, "limit", None) and args.limit > 0:
        companies = companies[:args.limit]
        logger.info(f"限制處理 {args.limit} 家公司。")

    if getattr(args, "batch", None):
        batch_num, total_batches = map(int, args.batch.split("/"))
        chunk_size = len(companies) // total_batches
        remainder = len(companies) % total_batches
        # Distribute remainder evenly: first 'remainder' batches get 1 extra
        start = (batch_num - 1) * chunk_size + min(batch_num - 1, remainder)
        end = batch_num * chunk_size + min(batch_num, remainder)
        companies = companies[start:end]
        logger.info(f"批次 {batch_num}/{total_batches}: 處理第 {start+1}-{end} 家公司 ({len(companies)} 家)")

    return companies
