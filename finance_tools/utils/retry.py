import logging
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_RETRY_DELAYS = (5, 15, 30)  # 秒，最多 3 次重試


def retry(fn: Callable[[], T], label: str) -> T:
    """執行 fn()，失敗（回傳 None 或拋出例外）時最多重試 3 次（5s/15s/30s）。
    所有重試後仍有例外則重新拋出；無資料（None）則回傳 None。
    """
    last_exc: Exception | None = None
    all_delays = list(_RETRY_DELAYS) + [None]
    for i, delay in enumerate(all_delays):
        try:
            result = fn()
            if result is not None:
                return result
            last_exc = None
        except Exception as e:
            last_exc = e
            logger.error(f"{label} error: {e}")
        if delay is not None:
            logger.warning(f"{label} 第 {i + 1} 次失敗，{delay}s 後重試...")
            time.sleep(delay)
    logger.error(f"{label} 全部重試失敗")
    if last_exc is not None:
        raise last_exc
    return None
