import json
import logging
import sys
from datetime import timedelta
from pathlib import Path

from finance_tools.core.timezone import now_tw
from finance_tools.domains.market_sentiment.fetcher import MarketSentimentFetcher

logger = logging.getLogger(__name__)

SENTIMENT_FILE = Path("src/data/market/sentiment.json")


def run_update_market_sentiment(args):
    """
    每日產出 src/data/market/sentiment.json：
      - institutional: 三大法人整體買賣超（TWSE BFI82U，單位：元）
      - margin: 融資融券整體市場加總（TWSE MI_MARGN + TPEx，單位：張）

    日期邏輯：
      18:00 前 → 抓前一日（證交所通常 17-18 時更新）
      18:00 後 → 抓今日
    """
    date_str = getattr(args, "date", None)
    if not date_str:
        now = now_tw()
        if now.hour < 18:
            date_str = (now - timedelta(days=1)).strftime("%Y%m%d")
        else:
            date_str = now.strftime("%Y%m%d")
    date_str = date_str.replace("-", "")

    logger.info("更新市場情緒數據，目標日期: %s", date_str)

    fetcher = MarketSentimentFetcher()
    data = fetcher.fetch_all(date_str)

    if not data:
        logger.error("無法取得任何市場情緒數據，中止。")
        sys.exit(1)

    SENTIMENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SENTIMENT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logger.info("已寫入 %s", SENTIMENT_FILE)
