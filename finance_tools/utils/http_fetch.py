"""GET 一整份檔案，被對方中途切斷時用 Range 從斷點續傳。

起因：櫃買（www.tpex.org.tw）2026-09-12 起，大一點的 openapi 回應會傳到一半就斷線——
`Content-Length: 847144`，實際只收到 16KB 或 200KB 左右，urllib 報 `IncompleteRead`，
requests 報 `ChunkedEncodingError`。**不是網路抖動**：同一支端點連打幾次都斷，
斷點還常落在同一個位置；小的端點（例如興櫃 142KB）則每次都完整。

斷法有兩種，都要接得住：
- 對方提早收尾 → `IncompleteRead`（例外裡帶著已收到的部分）
- 讀到一半連線被重置 → `ConnectionResetError`。**這種不帶已收資料**，所以一定要分段讀、
  收到就先存；一次 `read()` 整份的話，斷線時已收的 200KB 會跟著例外一起丟掉。

整份重抓沒有用（大檔幾乎每次都斷），但伺服器回 `Accept-Ranges: bytes`，
`Range: bytes=N-` 從斷點接著拿會回 206。實測外資持股 308KB 分 6 段拿完，
其他幾支多半 1~2 段。

用法：抓櫃買一律改用 `get_json`／`get_bytes`，不要自己 `urlopen(...).read()`——
那樣斷線就只能整份重來，重試再多次也可能一直斷在同一個地方。

**只做 GET、只做續傳**。不做重試策略（呼叫端各自已有，語意也不同）、不做快取。
第一次請求就失敗（還沒拿到任何 byte）時照常拋出，交給呼叫端既有的重試。
"""
import http.client
import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

# 一份檔案最多續傳幾段。實測最多 6 段；超過代表對方行為又變了，該讓人看到而不是無限接下去。
MAX_RESUMES = 30

# 分段讀的大小。只影響斷線時「已收的保得住多少」，不影響速度。
_CHUNK = 64 * 1024

_CONTENT_RANGE_TOTAL = re.compile(r"/(\d+)\s*$")


def get_bytes(url: str, headers: Optional[Mapping[str, str]] = None, timeout: float = 30) -> bytes:
    buf = bytearray()
    total: Optional[int] = None

    for _ in range(MAX_RESUMES + 1):
        h = dict(headers or {})
        if buf:
            h["Range"] = f"bytes={len(buf)}-"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as resp:
                if buf and resp.status != 206:
                    # 對方不理 Range、整份從頭給：丟掉已收的，當成第一次
                    buf.clear()
                    total = None
                if total is None:
                    total = _total_length(resp)
                try:
                    while True:
                        chunk = resp.read(_CHUNK)
                        if not chunk:
                            break
                        buf += chunk
                except http.client.IncompleteRead as e:
                    buf += e.partial
                    logger.info(f"續傳：{_short(url)} 收到 {len(buf)}/{total} bytes，從斷點接著拿")
                    continue
        except (urllib.error.URLError, OSError) as e:
            # OSError 涵蓋 ConnectionResetError／逾時／SSL 讀取中斷
            if not buf:
                raise  # 一個 byte 都沒拿到：不是續傳的問題，交給呼叫端的重試
            logger.info(f"續傳：{_short(url)} 在 {len(buf)} bytes 處連線中斷（{e}），再接一次")
            continue

        if total is None or len(buf) >= total:
            return bytes(buf)
        # 沒拋例外卻沒收滿（對方靜靜地關掉連線）：一樣從斷點接

    raise IOError(f"{url} 續傳 {MAX_RESUMES} 段仍未收完（{len(buf)}/{total} bytes）")


def get_json(url: str, headers: Optional[Mapping[str, str]] = None, timeout: float = 30) -> Any:
    return json.loads(get_bytes(url, headers=headers, timeout=timeout).decode("utf-8"))


def _total_length(resp) -> Optional[int]:
    """整份檔案的大小。206 要看 Content-Range 的分母，Content-Length 只是這一段的長度。"""
    if resp.status == 206:
        m = _CONTENT_RANGE_TOTAL.search(resp.headers.get("Content-Range", ""))
        return int(m.group(1)) if m else None
    cl = resp.headers.get("Content-Length")
    return int(cl) if cl and cl.isdigit() else None


def _short(url: str) -> str:
    return url.split("?")[0].rsplit("/", 1)[-1]
