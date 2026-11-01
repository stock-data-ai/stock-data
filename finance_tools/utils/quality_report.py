import os
import finance_tools.config as config


def save_quality_report(task_name: str, batch, quality_issues: list) -> None:
    """
    儲存或清除品質報告檔案。
    - 若有 quality_issues，寫入 finance_tools/quality_report_{task_name}_{batch}.txt
    - 若無 quality_issues，刪除舊報告（如果存在）
    """
    report_path = os.path.join(config.RERUN_DIR, f"quality_report_{task_name}_{batch}.txt")
    if quality_issues:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(quality_issues) + "\n")
    elif os.path.exists(report_path):
        os.remove(report_path)