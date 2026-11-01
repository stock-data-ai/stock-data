import json
import logging
import sys
from datetime import timedelta
from pathlib import Path
from typing import Optional

from finance_tools.core.timezone import now_tw
from finance_tools.core.trading_day import is_tw_trading_day, parse_yyyymmdd
from finance_tools.domains.market_sentiment.fetcher import MarketSentimentFetcher

logger = logging.getLogger(__name__)

SENTIMENT_FILE = Path("src/data/market/sentiment.json")
HISTORY_LIMIT = 10

MARGIN_HISTORY_FILE = Path("src/data/market/margin_history.json")
MARGIN_HISTORY_DAYS = 730  # 兩年水位，前端切 1M/3M/6M/1Y 用（留一年 buffer 給區間頭）

INST_HISTORY_FILE = Path("src/data/market/institutional_history.json")
INST_HISTORY_DAYS = 730  # 同上；前端在選定區間內自行累加成累計買賣超曲線


def _merge_history(existing_section: dict, history_limit: int) -> list:
    """
    把 existing_section 的當日資料 push 進 history 最前面，
    回傳去重且限長的 history list。
    """
    prev_history = existing_section.get("history", [])
    new_entry = {k: v for k, v in existing_section.items() if k != "history"}
    deduped = [e for e in prev_history if e.get("date") != new_entry.get("date")]
    return ([new_entry] + deduped)[:history_limit]


def _write_margin_history(fetcher: MarketSentimentFetcher, margin_section: Optional[dict]) -> None:
    """
    重建 src/data/market/margin_history.json：上市整體融資／融券「餘額水位」兩年日線。

    為什麼另存一支檔：sentiment.json 的 history 只留 10 筆（畫每日增減用），
    畫不出水位趨勢；把上限拉大會讓每日焦點頁必載的 sentiment.json 一路變胖。

    每次整段重抓 = 自我修復，某天漏跑不會在序列裡留缺口。
    當日那筆以剛抓到的 TWSE 官方數字覆蓋，確保線圖末端與資券卡上的餘額一致。
    FinMind 掛掉時保留既有檔案，只補當日一筆。
    """
    end = now_tw()
    start = end - timedelta(days=MARGIN_HISTORY_DAYS)
    start_str, end_str = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    series = fetcher.fetch_twse_margin_history(start_str, end_str)

    if series is None:
        if not MARGIN_HISTORY_FILE.exists():
            logger.warning("融資水位長序列取得失敗且無既有檔案，略過 margin_history.json")
            return
        logger.warning("融資水位長序列取得失敗，沿用既有檔案並只補當日")
        with open(MARGIN_HISTORY_FILE, encoding="utf-8") as f:
            series = json.load(f).get("series", [])

    twse = (margin_section or {}).get("twse") or {}
    today_point = {
        "date": (margin_section or {}).get("date"),
        "longAmount": twse.get("longAmount", {}).get("balance"),
        "longBalance": twse.get("longBalance", {}).get("balance"),
        "shortBalance": twse.get("shortBalance", {}).get("balance"),
    }
    if all(v is not None for v in today_point.values()):
        series = [p for p in series if p.get("date") != today_point["date"]] + [today_point]

    series = sorted(
        (p for p in series if p.get("date", "") >= start_str),
        key=lambda p: p["date"],
    )
    if not series:
        logger.warning("融資水位長序列為空，略過寫檔（保留既有 margin_history.json）")
        return

    payload = {
        "updated": end.strftime("%Y-%m-%d"),
        "market": "twse",  # 上市，與資券卡口徑一致（上櫃暫停）
        "units": {"longAmount": "元", "longBalance": "張", "shortBalance": "張"},
        "series": series,
    }
    MARGIN_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(MARGIN_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info(
        "已寫入 %s（%d 筆，%s ~ %s）",
        MARGIN_HISTORY_FILE, len(series), series[0]["date"], series[-1]["date"],
    )


def _write_institutional_history(
    fetcher: MarketSentimentFetcher, inst_section: Optional[dict]
) -> None:
    """
    重建 src/data/market/institutional_history.json：上市三大法人每日買賣超兩年日線。

    法人沒有「餘額」這種存量，前端的水位圖是在選定區間內自行累加（累計買賣超），
    所以這裡存的是每日淨額原始值，不預先累加——累加起點跟著使用者選的區間走。

    其餘策略與融資水位相同：整段重抓自我修復、當日以 BFI82U 官方數字覆蓋、
    FinMind 掛掉時保留既有檔案。
    """
    end = now_tw()
    start = end - timedelta(days=INST_HISTORY_DAYS)
    start_str, end_str = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    series = fetcher.fetch_twse_institutional_history(start_str, end_str)

    if series is None:
        if not INST_HISTORY_FILE.exists():
            logger.warning("三大法人長序列取得失敗且無既有檔案，略過 institutional_history.json")
            return
        logger.warning("三大法人長序列取得失敗，沿用既有檔案並只補當日")
        with open(INST_HISTORY_FILE, encoding="utf-8") as f:
            series = json.load(f).get("series", [])

    fields = ("foreign", "trust", "dealer", "dealerHedge", "foreignDealer")
    twse = (inst_section or {}).get("twse") or {}
    if twse and (inst_section or {}).get("date"):
        today_point = {"date": inst_section["date"]}
        today_point.update({f: twse.get(f, {}).get("net", 0) for f in fields})
        series = [p for p in series if p.get("date") != today_point["date"]] + [today_point]

    series = sorted(
        (p for p in series if p.get("date", "") >= start_str),
        key=lambda p: p["date"],
    )
    if not series:
        logger.warning("三大法人長序列為空，略過寫檔（保留既有 institutional_history.json）")
        return

    payload = {
        "updated": end.strftime("%Y-%m-%d"),
        "market": "twse",  # 上市，與三大法人卡口徑一致（上櫃暫停）
        "units": {f: "元" for f in fields},
        "series": series,
    }
    INST_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(INST_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info(
        "已寫入 %s（%d 筆，%s ~ %s）",
        INST_HISTORY_FILE, len(series), series[0]["date"], series[-1]["date"],
    )


def run_update_margin_history(args):
    """
    單獨重建融資水位／三大法人長序列（回補／修復用，任何一天跑都安全）。
    當日數字沿用現有 sentiment.json，不重打 TWSE。
    """
    existing: dict = {}
    if SENTIMENT_FILE.exists():
        with open(SENTIMENT_FILE, encoding="utf-8") as f:
            existing = json.load(f)
    fetcher = MarketSentimentFetcher()
    _write_margin_history(fetcher, existing.get("margin"))
    _write_institutional_history(fetcher, existing.get("institutional"))


def run_update_market_sentiment(args):
    """
    每日產出 src/data/market/sentiment.json：
      - institutional: 三大法人整體買賣超（TWSE BFI82U，單位：元）
      - margin: 融資融券整體市場加總（TWSE MI_MARGN + TPEx，單位：張）

    日期邏輯：
      一律抓今日。今日資料尚未公布（或休市）→ 跳過本次更新，保留既有資料。
      一天排多趟（15:55/16:55/20:55/21:55），哪一趟抓到就哪一趟更新。

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

    # 法人與資券公布時間不同（法人 ~15:00、資券 ~21:00），成敗各自獨立
    if data.get("institutional", {}).get("twse") is None:
        data.pop("institutional", None)

    # 交易日的 STALE 檢查：拿不到當日資料不可綠燈（休市日照舊跳過）。
    # 三大法人 ~15:00 公布，每一趟（15:55 起）都必須有；
    # 融資融券 ~21:00 公布，只在 21:30 後的那趟（21:55）強制要求。
    now = now_tw()
    is_trading = is_tw_trading_day(parse_yyyymmdd(date_str))
    is_today = date_str == now.strftime("%Y%m%d")

    if not data:
        if is_trading:
            logger.error("STALE: %s 為交易日，但完全抓不到任何市場情緒數據，終止任務。", date_str)
            sys.exit(1)
        logger.warning("目標日期 %s 尚無任何市場情緒數據（休市），跳過本次更新，保留既有資料。", date_str)
        return

    if is_trading and "institutional" not in data:
        logger.error("STALE: %s 為交易日，但三大法人整體買賣超尚未公布或抓取失敗，終止任務。", date_str)
        sys.exit(1)
    if is_trading and is_today and "margin" not in data and (now.hour, now.minute) >= (21, 30):
        logger.error("STALE: %s 為交易日且已過 21:30，但融資融券整體數據仍未取得，終止任務。", date_str)
        sys.exit(1)

    # 只有一邊抓到時，另一邊保留現有資料，不互相牽連
    if "institutional" not in data and "institutional" in existing:
        logger.warning("目標日期 %s institutional 尚未公布或抓取失敗，保留現有資料", date_str)
        data["institutional"] = existing["institutional"]
    if "margin" not in data and "margin" in existing:
        logger.warning("目標日期 %s margin 尚未公布或抓取失敗，保留現有資料", date_str)
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

    # 長序列是附加資料（水位／累計圖用），失敗不影響本任務主結果
    try:
        _write_margin_history(fetcher, data.get("margin"))
    except Exception:
        logger.exception("寫入融資水位長序列失敗，略過")
    try:
        _write_institutional_history(fetcher, data.get("institutional"))
    except Exception:
        logger.exception("寫入三大法人長序列失敗，略過")
