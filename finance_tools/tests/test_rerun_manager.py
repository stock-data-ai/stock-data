"""
RerunManager 單元測試 + 各 task module import 驗證

執行方式:
  cd finance_tools && python -m pytest tests/test_rerun_manager.py -v
  或
  cd finance_tools && python tests/test_rerun_manager.py
"""
import os
import sys
import tempfile
import unittest

# Ensure finance_tools is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.rerun_manager import RerunManager


class TestRerunManager(unittest.TestCase):
    """RerunManager 核心功能測試"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        # Monkey-patch RERUN_DIR for testing
        import utils.rerun_manager as mod
        self._orig_dir = mod.RERUN_DIR
        mod.RERUN_DIR = self.test_dir

    def tearDown(self):
        import utils.rerun_manager as mod
        mod.RERUN_DIR = self._orig_dir
        # Clean up temp files
        for f in os.listdir(self.test_dir):
            os.remove(os.path.join(self.test_dir, f))
        os.rmdir(self.test_dir)

    # --- file_path 命名規則 ---

    def test_file_path_no_batch(self):
        mgr = RerunManager("valuation")
        self.assertEqual(mgr.file_path, os.path.join(self.test_dir, "rerun_queue_valuation.txt"))

    def test_file_path_with_batch(self):
        mgr = RerunManager("full_update", batch="2")
        self.assertEqual(mgr.file_path, os.path.join(self.test_dir, "rerun_queue_full_update_2.txt"))

    # --- save / load ---

    def test_save_and_load(self):
        mgr = RerunManager("test_task")
        mgr.save(["2330", "1101", "2454"])
        loaded = mgr.load()
        self.assertEqual(loaded, ["1101", "2330", "2454"])  # sorted

    def test_save_deduplicates(self):
        mgr = RerunManager("test_task")
        mgr.save(["2330", "1101", "2330", "1101"])
        loaded = mgr.load()
        self.assertEqual(loaded, ["1101", "2330"])

    def test_save_empty_clears_file(self):
        mgr = RerunManager("test_task")
        mgr.save(["2330"])
        self.assertTrue(os.path.exists(mgr.file_path))
        mgr.save([])  # empty list should clear
        self.assertFalse(os.path.exists(mgr.file_path))

    def test_load_nonexistent(self):
        mgr = RerunManager("nonexistent")
        self.assertEqual(mgr.load(), [])

    # --- save_api_exhausted ---

    def test_save_api_exhausted(self):
        mgr = RerunManager("test_task")
        failed = ["1101"]
        current = "2330"
        remaining = [{"code": "2454"}, {"code": "3008"}]
        mgr.save_api_exhausted(failed, current, remaining)
        loaded = mgr.load()
        self.assertEqual(loaded, ["1101", "2330", "2454", "3008"])

    # --- clear ---

    def test_clear_removes_file(self):
        mgr = RerunManager("test_task")
        mgr.save(["2330"])
        self.assertTrue(os.path.exists(mgr.file_path))
        mgr.clear()
        self.assertFalse(os.path.exists(mgr.file_path))

    def test_clear_no_error_if_not_exists(self):
        mgr = RerunManager("test_task")
        mgr.clear()  # should not raise

    # --- has_failures ---

    def test_has_failures_true(self):
        mgr = RerunManager("test_task")
        mgr.save(["2330"])
        self.assertTrue(mgr.has_failures())

    def test_has_failures_false_no_file(self):
        mgr = RerunManager("test_task")
        self.assertFalse(mgr.has_failures())

    def test_has_failures_false_after_clear(self):
        mgr = RerunManager("test_task")
        mgr.save(["2330"])
        mgr.clear()
        self.assertFalse(mgr.has_failures())

    # --- 不同 task 名稱不衝突 ---

    def test_different_tasks_independent(self):
        mgr_a = RerunManager("valuation")
        mgr_b = RerunManager("shareholder")
        mgr_a.save(["1101"])
        mgr_b.save(["2330"])
        self.assertEqual(mgr_a.load(), ["1101"])
        self.assertEqual(mgr_b.load(), ["2330"])
        mgr_a.clear()
        self.assertFalse(mgr_a.has_failures())
        self.assertTrue(mgr_b.has_failures())  # b is still there


class TestImports(unittest.TestCase):
    """驗證所有 task module 的 import 不會報錯（不需要 API key）"""

    def test_import_rerun_manager(self):
        from utils.rerun_manager import RerunManager
        self.assertIsNotNone(RerunManager)

    def test_import_company_list_loader(self):
        from utils.company_list_loader import load_companies_for_processing
        self.assertIsNotNone(load_companies_for_processing)

    def test_import_full_update(self):
        from tasks.full_update import run_full_update
        self.assertIsNotNone(run_full_update)

    def test_import_update_valuation(self):
        from tasks.update_valuation import run_update_valuation
        self.assertIsNotNone(run_update_valuation)

    def test_import_update_institutional_investors(self):
        from tasks.update_institutional_investors import run_update_institutional_investors
        self.assertIsNotNone(run_update_institutional_investors)

    def test_import_fetch_shareholder_data(self):
        from tasks.fetch_shareholder_data import run_fetch_shareholder_data
        self.assertIsNotNone(run_fetch_shareholder_data)

    def test_import_update_revenue(self):
        from tasks.update_revenue import run_update_revenue
        self.assertIsNotNone(run_update_revenue)

    def test_import_update_stock_prices(self):
        from tasks.update_stock_prices import run_update_stock_prices
        self.assertIsNotNone(run_update_stock_prices)

    def test_import_check_quality(self):
        from tasks.check_quality import run_check_quality
        self.assertIsNotNone(run_check_quality)


if __name__ == "__main__":
    unittest.main()
