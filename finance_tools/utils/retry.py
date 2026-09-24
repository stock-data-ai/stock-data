import logging
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 秒，最多 5 次重試（共 6 次嘗試，約 68 秒）。TWSE 邊緣快取會間歇回空資料，
# 呼叫端已加 cache buster，但仍需多幾次才夠把壞快取磨掉（見 utils/twse_url.py）。
_RETRY_DELAYS = (3, 5, 10, 20, 30)


def retry(fn: Callable[[], T], label: str) -> T:
    """執行 fn()，失敗（回傳 None 或拋出例外）時最多重試 5 次（3/5/10/20/30s）。
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
