"""全市場抓取（`core/market_fetch.py`）：每一期一次請求，失敗就整個停。"""

from datetime import date

import pandas as pd
import pytest

from finance_tools.core import market_fetch
from finance_tools.core.data_processor import DataProcessor
from finance_tools.core.exceptions import ApiExhaustedError


def test_quarter_end_dates_start_at_q1_and_stop_at_today():
    """視窗前緣一定是 Q1：現金流量表單季要靠「本季－上一季累計」，前緣落在 Q2 會拆不出來。"""
    got = market_fetch.quarter_end_dates(1, date(2026, 10, 22))
    assert got[0] == "2025-03-31"
    assert got[-1] == "2026-09-30", "還沒到的季末不抓"
    assert len(got) == 7


def test_month_start_dates_cover_january_to_today():
    got = market_fetch.month_start_dates(0, date(2026, 10, 22))
    assert got[0] == "2026-01-01" and got[-1] == "2026-10-01" and len(got) == 10


def test_item_filter_keeps_wanted_items_and_drops_percent_rows():
    keep = market_fetch.item_filter(DataProcessor.BALANCE_SHEET_ITEMS)
    df = pd.DataFrame([
        {"stock_id": "8103", "type": "Inventories", "origin_name": "存貨", "value": 591970000.0},
        {"stock_id": "8103", "type": "Inventories_per", "origin_name": "存貨", "value": 8.57},
        {"stock_id": "8103", "type": "Cash", "origin_name": "現金及約當現金", "value": 1.0},
        # 半形括號的寫法也要認得（比對前正規化）
        {"stock_id": "8103", "type": "X", "origin_name": "應收帳款－關係人淨額", "value": 2649000.0},
    ])
    out = keep(df)
    assert list(out["value"]) == [591970000.0, 2649000.0]


class _Client:
    def __init__(self, fail_on=None, exc=ApiExhaustedError):
        self.fail_on, self.exc, self.asked = fail_on, exc, []

    def fetch_market_day(self, dataset, d):
        self.asked.append(d)
        if d == self.fail_on:
            raise self.exc(f"{dataset} {d}")
        if d == "2026-09-30":
            return pd.DataFrame()  # 季報還沒公布：空，不是錯
        return pd.DataFrame([{"stock_id": "8103", "date": d, "value": 1.0},
                             {"stock_id": "2330", "date": d, "value": 2.0}])


def test_fetch_market_one_request_per_period_and_split_by_stock():
    client = _Client()
    dates = ["2026-03-31", "2026-06-30", "2026-09-30"]
    df = market_fetch.fetch_market(client, "TaiwanStockBalanceSheet", dates)
    assert sorted(client.asked) == dates, "每一期剛好一次請求"
    by_code = market_fetch.split_by_stock(df)
    assert set(by_code) == {"8103", "2330"}
    assert list(by_code["8103"]["date"]) == ["2026-03-31", "2026-06-30"]


@pytest.mark.parametrize("exc", [ApiExhaustedError, RuntimeError])
def test_any_failed_period_stops_the_whole_fetch(exc):
    """抓不完整就不往下走：呼叫端拿不到半套資料，也就不會寫檔。"""
    with pytest.raises(exc):
        market_fetch.fetch_market(_Client(fail_on="2026-06-30", exc=exc), "X",
                                  ["2026-03-31", "2026-06-30"])
