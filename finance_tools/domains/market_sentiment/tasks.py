import json
import logging
from pathlib import Path

from finance_tools.core.timezone import now_tw
from finance_tools.domains.market_sentiment.fetcher import MarketSentimentFetcher

logger = logging.getLogger(__name__)

SENTIMENT_FILE = Path("src/data/market/sentiment.json")
HISTORY_LIMIT = 10


def _merge_history(existing_section: dict, history_limit: int) -> list:
    """
    把 existing_section 的當日資料 push 進 history 最前面，
    回傳去重且限長的 history list。
    """
    prev_history = existing_section.get("history", [])
    new_entry = {k: v for k, v in existing_section.items() if k != "history"}
    deduped = [e for e in prev_history if e.get("date") != new_entry.get("date")]
    return ([new_entry] + deduped)[:history_limit]


def run_update_market_sentiment(args):
    """
    每日產出 src/data/market/sentiment.json：
      - institutional: 三大法人整體買賣超（TWSE BFI82U，單位：元）
      - margin: 融資融券整體市場加總（TWSE MI_MARGN + TPEx，單位：張）

    日期邏輯：
      一律抓今日。今日資料尚未公布（或休市）→ 跳過本次更新，保留既有資料。
      一天排多趟（15:55/16:55/19:55），哪一趟抓到就哪一趟更新。

    History 邏輯：
      每次寫入前，把現有資料 push 進 history（最多 HISTORY_LIMIT 筆）。
      同日重跑時保留現有 history 不重複累積。
    """
    date_str = getattr(args, "date", None)
    if not date_str:
        date_str = now_tw().strftime("%Y%m%d")
    date_str = date_str.replace("-", "")
    formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

    logger.info("更新市場情緒數據，目標日期: %s", date_str)

    # 讀取現有資料（若存在）
    existing: dict = {}
    if SENTIMENT_FILE.exists():
        try:
            with open(SENTIMENT_FILE, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            logger.warning("無法讀取現有 sentiment.json，history 將從空白開始")

    fetcher = MarketSentimentFetcher()
    data = fetcher.fetch_all(date_str)

    if not data:
        logger.warning("目標日期 %s 尚無任何市場情緒數據（尚未公布或休市），跳過本次更新，保留既有資料。", date_str)
        return

    # TWSE 必須成功才寫入（TPEX 上櫃暫停）
    inst = data.get("institutional", {})
    if inst.get("twse") is None:
        logger.warning("目標日期 %s institutional 缺少 twse（尚未公布或休市），跳過本次更新，保留既有資料。", date_str)
        return

    # margin 抓失敗時保留現有資料（非強制）
    if "margin" not in data and "margin" in existing:
        logger.warning("margin fetch 失敗，保留現有資料")
        data["margin"] = existing["margin"]

    # 判斷是否為同日重跑
    existing_date = (
        existing.get("institutional", {}).get("date")
        or existing.get("margin", {}).get("date")
    )
    same_day = existing_date == formatted_date

    for section in ("institutional", "margin"):
        if section not in data:
            continue
        if section in existing:
            if same_day:
                data[section]["history"] = existing[section].get("history", [])
                # 保留 existing 中有、但本次 API 失敗而缺少的子欄位（twse/tpex）
                for sub in ("twse", "tpex"):
                    if sub not in data[section] and sub in existing[section]:
                        logger.warning(
                            "%s.%s 本次 API 失敗，保留上次成功資料", section, sub
                        )
                        data[section][sub] = existing[section][sub]
            else:
                data[section]["history"] = _merge_history(
                    existing[section], HISTORY_LIMIT
                )
        else:
            data[section]["history"] = []

    SENTIMENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SENTIMENT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logger.info("已寫入 %s（history 筆數：inst=%d margin=%d）",
                SENTIMENT_FILE,
                len(data.get("institutional", {}).get("history", [])),
                len(data.get("margin", {}).get("history", [])))
