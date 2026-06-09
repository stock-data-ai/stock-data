import logging
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_RETRY_DELAYS = (5, 15, 30)  # 秒，最多 3 次重試


def retry(fn: Callable[[], T], label: str) -> T:
    """執行 fn()，失敗（回傳 None）時最多重試 3 次（5s/15s/30s）。"""
    for attempt, delay in enumerate(_RETRY_DELAYS, 1):
        result = fn()
        if result is not None:
            return result
        logger.warning(f"{label} 第 {attempt} 次失敗，{delay}s 後重試...")
        time.sleep(delay)
    result = fn()
    if result is None:
        logger.error(f"{label} 全部重試失敗")
    return result
