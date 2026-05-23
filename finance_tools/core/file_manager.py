"""
File Manager - 統一的檔案管理
"""

import os
import json
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)
from finance_tools.core.timezone import today_str
import finance_tools.config as config


class FileManager:
    """檔案管理器"""

    def __init__(self, base_dir: str = None):
        self.base_dir = base_dir or str(config.BASE_DIR)
        self.companies_dir = str(config.COMPANIES_DIR)
        self.company_topics_base_dir = str(config.COMPANY_TOPICS_DIR)
        self.financials_dir = str(config.COMPANY_FINANCIALS_DIR)

        # 確保目錄存在
        os.makedirs(self.financials_dir, exist_ok=True)
        os.makedirs(self.company_topics_base_dir, exist_ok=True) # Ensure this also exists

    def load_companies(self) -> List[Dict[str, str]]:
        """
        載入公司清單。從 company-financials/ 目錄掃描所有 4 位數台股代碼，
        並從 companies-all.json 取得公司名稱。
        這樣可以覆蓋所有已有財務檔案的公司，不論是否在 company-topics 題材中。
        """
        all_companies_details = self.load_all_companies_with_details()

        codes: set[str] = set()
        if os.path.isdir(self.financials_dir):
            for filename in os.listdir(self.financials_dir):
                if filename.endswith(".json"):
                    code = filename[:-5]  # strip .json
                    if code.isdigit() and len(code) == 4:
                        codes.add(code)

        if not codes:
            logger.warning(f"No 4-digit company codes found in {self.financials_dir}.")
            return []

        companies = []
        for code in sorted(codes):
            detail = all_companies_details.get(code, {})
            name = detail.get("name", code)
            companies.append({"code": code, "name": name})

        logger.info(f"Loaded {len(companies)} companies from {self.financials_dir}.")
        return companies

    def load_all_companies_with_details(self) -> Dict[str, Any]:
        """
        載入 companies-all.json 檔案以獲取完整的公司詳細資訊。
        """
        file_path = str(config.COMPANIES_ALL_FILE)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            logger.error(f"Error: companies-all.json not found at {file_path}")
            return {}
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding JSON from {file_path}: {e}")
            return {}
        except Exception as e:
            logger.error(f"Error reading {file_path}: {e}")
            return {}

    def save_all_companies_data(self, data: Dict[str, Any]) -> bool:
        """
        儲存所有公司的資料到 companies-all.json
        """
        import tempfile
        output_path = str(config.COMPANIES_ALL_FILE)
        try:
            fd, temp_path = tempfile.mkstemp(
                dir=os.path.dirname(output_path),
                prefix=".companies-all_",
                suffix=".json.tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, output_path)
            except Exception:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise
            logger.info(f"✅ Successfully saved all company data to {output_path}")
            return True
        except Exception as e:
            logger.error(f"Error saving all companies data to {output_path}: {e}")
            return False

    def save_financial_data(self, code: str, data: Dict[str, Any]) -> bool:
        """儲存財務數據（使用原子寫入）"""
        import tempfile
        try:
            output_path = os.path.join(self.financials_dir, f"{code}.json")

            # 使用原子寫入：先寫入臨時檔案，然後原子性地重命名
            # 這樣即使寫入過程中出錯，原檔案也不會損壞
            fd, temp_path = tempfile.mkstemp(
                dir=self.financials_dir,
                prefix=f".{code}_",
                suffix=".json.tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())  # 確保數據真正寫入磁盤

                # 原子性地重命名臨時檔案到目標檔案
                # 在 POSIX 系統上，這是原子操作
                os.replace(temp_path, output_path)
                return True
            except Exception:
                # 如果出錯，清理臨時檔案
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise
        except Exception as e:
            logger.error(f"Error saving data for {code}: {e}")
            return False

    def load_financial_data(self, code: str) -> Dict[str, Any]:
        """載入財務數據"""
        try:
            file_path = os.path.join(self.financials_dir, f"{code}.json")
            if not os.path.exists(file_path):
                return {}

            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading data for {code}: {e}")
            return {}

    def file_exists(self, code: str) -> bool:
        """檢查檔案是否存在"""
        file_path = os.path.join(self.financials_dir, f"{code}.json")
        return os.path.exists(file_path)

    def is_updated_today(self, code: str) -> bool:
        """檢查檔案的 lastUpdated 是否為今天日期"""
        data = self.load_financial_data(code)
        if not data:
            return False
        last_updated = data.get("lastUpdated", "")
        return last_updated == today_str()

    def delete_file(self, code: str) -> bool:
        """刪除檔案"""
        try:
            file_path = os.path.join(self.financials_dir, f"{code}.json")
            if os.path.exists(file_path):
                os.remove(file_path)
                return True
            return False
        except Exception as e:
            logger.error(f"Error deleting file for {code}: {e}")
            return False