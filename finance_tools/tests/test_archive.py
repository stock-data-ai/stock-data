"""歷史封存（`archive-history`）：把舊年度搬進 archive，不是刪掉。

三件事一定要成立，否則這支就是在毀資料：
  1. 搬走的每一筆都要在封存檔裡找得到
  2. dry-run 不得寫任何檔
  3. 重跑要冪等，而且封存檔的 **bytes** 不能變
     （變了的話 git 每次都會存一份完整新 blob，一年下來多好幾 GB）
"""

import gzip
import json
from pathlib import Path

import pytest

import finance_tools.scripts.archive_historical_data as archive


def sample(code, years=(2020, 2023, 2025, 2026)):
    return {
        "companyCode": code,
        "companyName": "測試公司",
        "historical": {
            "annual": [{"year": 2025, "eps": 1.0}],
            "institutionalInvestors": {
                f"{y}-06-1{i}": {"foreign_net_buy": float(y + i)}
                for y in years for i in range(3)
            },
            "marginTrading": {f"{y}-06-15": {"marginBalance": y} for y in years},
            "securitiesLending": {f"{y}-06-15": {"sblBalance": y} for y in years},
        },
        "shareholderDataHistory": {f"{y}0615": [{"level": y}] for y in years},
        "lastUpdated": "2026-01-01",
    }


@pytest.fixture
def staged(isolated_file_manager, tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "ARCHIVE_DIR", tmp_path / "company-financials-archive")
    for code in ("1101", "2330"):
        isolated_file_manager.save_financial_data(code, sample(code))
    return isolated_file_manager


@pytest.fixture(autouse=True)
def _fixed_year(monkeypatch):
    class _Now:
        year = 2026
    monkeypatch.setattr(archive, "now_tw", lambda: _Now())


def test_dry_run_writes_nothing(staged, tmp_path):
    before = {c: staged.load_financial_data(c) for c in ("1101", "2330")}
    result = archive.run(dry_run=True)
    assert result["records"] > 0, "應該有東西可以搬，否則這個測試沒驗到"
    assert not (tmp_path / "company-financials-archive").exists()
    assert {c: staged.load_financial_data(c) for c in ("1101", "2330")} == before


def test_every_archived_record_is_recoverable(staged, tmp_path):
    before = {c: staged.load_financial_data(c) for c in ("1101", "2330")}
    archive.run()

    for code, orig in before.items():
        now = staged.load_financial_data(code)
        old_ii = orig["historical"]["institutionalInvestors"]
        new_ii = now["historical"]["institutionalInvestors"]
        moved = set(old_ii) - set(new_ii)
        assert moved, "應該有搬走的筆數"
        for key in moved:
            path = tmp_path / "company-financials-archive" / key[:4] / f"{code}.json.gz"
            with gzip.open(path, "rt", encoding="utf-8") as f:
                stored = json.load(f)
            assert stored["institutionalInvestors"][key] == old_ii[key], f"{code} {key} 對不上"


@pytest.mark.parametrize("field", ["institutionalInvestors", "marginTrading", "securitiesLending"])
def test_keeps_current_and_previous_year(staged, field):
    """四個日期集合走**同一套**規則——不會有一半封存、一半被刪。"""
    archive.run()
    kept = staged.load_financial_data("1101")["historical"][field]
    years = {k[:4] for k in kept}
    assert years == {"2025", "2026"}, f"{field} 應只留當年＋前一年，實際 {sorted(years)}"


@pytest.mark.parametrize("field", ["marginTrading", "securitiesLending"])
def test_daily_chip_history_is_archived_not_deleted(staged, tmp_path, field):
    """融資融券／借券的舊資料是**搬走**，不是刪掉。"""
    before = staged.load_financial_data("1101")["historical"][field]
    archive.run()
    after = staged.load_financial_data("1101")["historical"][field]
    moved = set(before) - set(after)
    assert moved, "應該有搬走的年度"
    for key in moved:
        path = tmp_path / "company-financials-archive" / key[:4] / "1101.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as f:
            assert json.load(f)[field][key] == before[key], f"{field} {key} 沒搬到封存"


def test_shareholder_history_is_archived_too(staged, tmp_path):
    archive.run()
    kept = staged.load_financial_data("1101")["shareholderDataHistory"]
    assert {k[:4] for k in kept} == {"2025", "2026"}
    path = tmp_path / "company-financials-archive" / "2020" / "1101.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        assert "20200615" in json.load(f)["shareholderDataHistory"]


def test_rerun_is_idempotent_down_to_the_bytes(staged, tmp_path):
    archive.run()
    arch_dir = tmp_path / "company-financials-archive"
    main_snapshot = {c: staged.load_financial_data(c) for c in ("1101", "2330")}
    gz_snapshot = {p: p.read_bytes() for p in arch_dir.rglob("*.gz")}
    assert gz_snapshot, "應該有封存檔"

    result = archive.run()

    assert result["records"] == 0, "第二次不該再搬任何東西"
    assert {c: staged.load_financial_data(c) for c in ("1101", "2330")} == main_snapshot
    assert {p: p.read_bytes() for p in arch_dir.rglob("*.gz")} == gz_snapshot, \
        "封存檔 bytes 變了 → git 每次都會存一份新 blob"


def test_corrupt_main_file_is_left_alone(isolated_file_manager, tmp_path, monkeypatch):
    """壞檔不封存也不改寫——它是 financials-update 要重抓的訊號。"""
    import os
    monkeypatch.setattr(archive, "ARCHIVE_DIR", tmp_path / "arch")
    isolated_file_manager.save_financial_data("9999", sample("9999"))
    path = os.path.join(isolated_file_manager.financials_dir, "9999.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"broken')

    archive.run()

    with open(path, encoding="utf-8") as f:
        assert f.read() == '{"broken', "壞檔必須原封不動"


def test_one_corrupt_archive_does_not_stop_the_others(staged, tmp_path):
    """一家的封存檔壞掉只跳過那一家，其餘照常封存；CLI 回非零讓 CI 亮紅燈。

    回歸測試：原本一個 raise 就整批中止，daily-update 排在後面的
    「寫當日市值」步驟也跟著不跑。
    """
    bad = tmp_path / "company-financials-archive" / "2020" / "1101.json.gz"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not gzip")
    before_1101 = staged.load_financial_data("1101")

    result = archive.run()

    assert result["failed"] == ["1101"]
    assert staged.load_financial_data("1101") == before_1101, "失敗的那家主檔不得動"
    assert bad.read_bytes() == b"not gzip", "壞掉的封存檔不得被覆寫"
    kept = staged.load_financial_data("2330")["historical"]["institutionalInvestors"]
    assert {k[:4] for k in kept} == {"2025", "2026"}, "其他公司照常封存"
    assert archive.main() == 1, "有失敗就回非零"


def test_cli_exit_code_is_zero_when_nothing_failed(staged):
    assert archive.main() == 0


def test_prune_removes_archive_together_with_the_main_file(isolated_file_manager, tmp_path, monkeypatch):
    """下市清除要把封存檔一起帶走——「不留墓碑」對主檔與封存是同一個政策。"""
    import finance_tools.scripts.prune_financials as prune

    monkeypatch.setattr(prune, "ARCHIVE_DIR", tmp_path / "arch")
    for year in ("2023", "2024"):
        d = tmp_path / "arch" / year
        d.mkdir(parents=True)
        (d / "9999.json.gz").write_bytes(b"x")
        (d / "2330.json.gz").write_bytes(b"y")
    isolated_file_manager.save_financial_data("9999", {"companyCode": "9999"})
    isolated_file_manager.save_financial_data("2330", {"companyCode": "2330"})

    monkeypatch.setattr(prune, "FileManager", lambda: isolated_file_manager)
    monkeypatch.setattr(isolated_file_manager, "load_companies",
                        lambda: [{"code": "2330", "name": "台積電"}])

    prune.run()

    assert not (tmp_path / "arch" / "2023" / "9999.json.gz").exists(), "下市公司的封存要一起刪"
    assert not (tmp_path / "arch" / "2024" / "9999.json.gz").exists()
    assert (tmp_path / "arch" / "2023" / "2330.json.gz").exists(), "在架上的公司不能被碰"


def test_prune_sees_files_whose_code_is_not_four_digits(isolated_file_manager, monkeypatch, tmp_path):
    """名單外的檔不論代號長相都要清——只看 4 碼時，006205／006208 兩個 ETF 測試檔躺了四個月。"""
    import os
    import finance_tools.scripts.prune_financials as prune

    monkeypatch.setattr(prune, "ARCHIVE_DIR", tmp_path / "arch")
    for code in ("2330", "006205", "006208"):
        isolated_file_manager.save_financial_data(code, {"companyCode": code})
    monkeypatch.setattr(prune, "FileManager", lambda: isolated_file_manager)
    monkeypatch.setattr(isolated_file_manager, "load_companies",
                        lambda: [{"code": "2330", "name": "台積電"}])

    prune.run()

    left = sorted(f[:-5] for f in os.listdir(isolated_file_manager.financials_dir) if f.endswith(".json"))
    assert left == ["2330"]
