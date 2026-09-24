import os
import logging
from typing import List, Dict, Optional
import finance_tools.config as config

logger = logging.getLogger(__name__)

RERUN_DIR = str(config.RERUN_DIR)


class RerunManager:
    """
    統一管理 rerun queue 檔案的讀寫與清理。

    每個 task 使用獨立的 rerun file，避免不同 task 互相覆蓋。
    檔案命名規則:
      - finance_tools/rerun_queue_{task_name}.txt
      - finance_tools/rerun_queue_{task_name}_{batch}.txt  (有 batch 時)
    """

    def __init__(self, task_name: str, batch: Optional[str] = None):
        """
        Args:
            task_name: 任務識別名稱，例如 "valuation", "financials_update"
            batch: batch 編號字串，例如 "1" (來自 --batch 1/4)，無 batch 時為 None
        """
        self.task_name = task_name
        self.batch = batch
        suffix = f"_{batch}" if batch else ""
        self._file_path = os.path.join(RERUN_DIR, f"rerun_queue_{task_name}{suffix}.txt")

    @property
    def file_path(self) -> str:
        return self._file_path

    def save(self, failed_codes: List[str]) -> None:
        """寫入失敗清單。空清單則清除檔案。"""
        unique_codes = sorted(set(failed_codes))
        if not unique_codes:
            self.clear()
            return
        with open(self._file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(unique_codes) + "\n")
        logger.warning(f"已儲存 {len(unique_codes)} 間失敗公司至: {self._file_path}")

    def save_api_exhausted(
        self,
        failed_codes: List[str],
        current_code: str,
        remaining_companies: List[Dict],
    ) -> None:
        """ApiExhaustedError 時的快速保存：合併已失敗 + 當前 + 剩餘公司。"""
        remaining_codes = [c["code"] for c in remaining_companies]
        all_failed = sorted(set(failed_codes + [current_code] + remaining_codes))
        with open(self._file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(all_failed) + "\n")
        logger.warning(f"API 額度耗盡，已將 {len(all_failed)} 間公司寫入: {self._file_path}")

    def load(self) -> List[str]:
        """讀取失敗公司清單。檔案不存在時回傳空清單。"""
        if not os.path.exists(self._file_path):
            logger.info(f"Rerun 檔案不存在: {self._file_path}")
            return []
        with open(self._file_path, "r", encoding="utf-8") as f:
            codes = [line.strip() for line in f if line.strip()]
        logger.info(f"從 {self._file_path} 載入 {len(codes)} 間公司。")
        return codes

    def clear(self) -> None:
        """清除 rerun file（全部成功時呼叫）。"""
        if os.path.exists(self._file_path):
            os.remove(self._file_path)
            logger.info(f"所有公司已成功處理，已移除: {self._file_path}")

    def has_failures(self) -> bool:
        """檢查是否有未處理的失敗。"""
        return os.path.exists(self._file_path) and os.path.getsize(self._file_path) > 0
