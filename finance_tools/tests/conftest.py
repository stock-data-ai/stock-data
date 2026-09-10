"""離線保護：財報管線測試不准打網路、不准開 subprocess、不准寫正式資料目錄。

`finance_tools/config.py` 的路徑是**相對路徑**（`Path("src/data/layer3")`），
所以在 repo root 跑測試時 `FileManager()` 會直接指到正式資料。這裡用 fixture
把三個目錄常數改指到 tmp_path，測試只要拿 `isolated_file_manager` 就不會碰到 src/data。
"""

import socket
import subprocess
import sys
from pathlib import Path

import pytest

# `finance_tools` 不是安裝進 venv 的套件，pytest 只會把 tests/ 放上 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import finance_tools.config as config


class BlockedIO(Exception):
    """測試期間有人嘗試網路／subprocess。"""


@pytest.fixture(autouse=True)
def _no_external_io(monkeypatch):
    def deny(*args, **kwargs):
        raise BlockedIO("financial pipeline tests run fully offline")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(subprocess, "Popen", deny)
    monkeypatch.setattr(subprocess, "run", deny)


@pytest.fixture(autouse=True)
def _isolate_data_paths(tmp_path, monkeypatch):
    """**autouse**：每一條測試都把資料路徑指到 tmp_path。

    這裡刻意是 fail-closed 而不是 opt-in。`config.py` 用的是相對路徑
    （`Path("src/data/layer3")`），在 repo root 跑測試時，任何一個自己 `FileManager()`
    的測試都會直接寫進正式資料——2026-09-10 就這樣在
    `src/data/layer3/company-financials/` 留下一個 `9999.json`。
    預設安全，才不會下一個人忘了取用 fixture 就中獎。
    """
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "COMPANY_FINANCIALS_DIR", tmp_path / "company-financials")
    monkeypatch.setattr(config, "COMPANY_TOPICS_DIR", tmp_path / "company-topics")
    monkeypatch.setattr(config, "COMPANIES_DIR", tmp_path / "companies")


@pytest.fixture
def isolated_file_manager(_isolate_data_paths, tmp_path):
    """真正的 FileManager，路徑已由 `_isolate_data_paths` 隔離。"""
    from finance_tools.core.file_manager import FileManager

    fm = FileManager()
    assert str(tmp_path) in fm.financials_dir, "FileManager 未被隔離，拒絕在正式資料上跑測試"
    return fm
