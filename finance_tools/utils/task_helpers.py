import sys
import logging

from finance_tools.utils.quality_report import save_quality_report

logger = logging.getLogger(__name__)


def handle_api_exhausted(
    task_name: str,
    batch,
    write_mgr,
    failed_companies: list,
    current_code: str,
    remaining_companies: list,
    success_count: int,
    total_count: int,
    quality_issues: list,
) -> None:
    """
    API 額度耗盡時的統一處理：
    1. 記錄 quality report
    2. 透過 write_mgr 保存尚未處理的公司
    3. 以 exit code 1 退出（讓 GitHub Actions 觸發下一輪重試）
    """
    logger.warning(f"\n⚠️  API 額度已耗盡，發生於公司 {current_code}。所有可用 token 皆已用盡。")
    save_quality_report(task_name, batch, quality_issues)
    write_mgr.save_api_exhausted(failed_companies, current_code, remaining_companies)
    logger.info(f"本輪已完成: {success_count}/{total_count} 間公司。")
    logger.info("退出，等待 GitHub Actions 觸發下一輪重試。")
    sys.exit(2)  # exit 2 = API exhausted (rerun needed), not a real error
