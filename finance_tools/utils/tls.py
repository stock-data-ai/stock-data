"""把 repo 自備的中繼憑證併進 CA bundle，讓所有 HTTPS 呼叫都吃得到。

起因：櫃買（www.tpex.org.tw）自 2026-09-12 起的對外節點只送伺服器憑證、不送簽發者的
中繼憑證。瀏覽器會依 AIA 自行補齊，Python 不會 —— 於是 urllib 與 requests 全部
CERTIFICATE_VERIFY_FAILED，重試再多次也一樣（不是網路抖動，是鏈本身不完整）。

**不是關掉驗證**：信任根仍是憑證庫裡既有的 TWCA 根憑證，只是把對方漏送的中繼憑證改由
我們自備。憑證過期／被撤銷／網域對不上，照樣會擋下來。

兩道手續，缺一不可：
1. 設 `SSL_CERT_FILE` 與 `REQUESTS_CA_BUNDLE` —— 涵蓋 requests 以及子行程。
2. 包一層 `ssl.create_default_context` —— **光靠環境變數在 macOS 上不生效**。
   實測 macOS 內建 Python 3.9：明確帶 cafile 會過，但 `create_default_context()` 不帶參數
   （urllib／http.client 正是走這條）仍然驗證失敗，因為它同時吃 `SSL_CERT_DIR` 指到的
   `/private/etc/ssl/certs`，補上的 bundle 被略過。包一層在沒給憑證來源時補 load，
   才是所有 client 都涵蓋到。

因此**必須在任何連線發生前執行**，掛在 `finance_tools/__init__.py`。逐一去改十幾個
fetcher 的 context 參數則是漏一支就繼續紅，且新寫的抓取程式還會再踩一次。

已設好的環境變數不覆蓋 —— CI 或使用者若刻意指定 bundle，那是更高層的決定。
"""
import atexit
import logging
import os
import ssl
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_EXTRA_CERTS_DIR = Path(__file__).parent.parent / "assets/certs"
_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")

_installed_path: Optional[str] = None


def install_ca_bundle() -> Optional[str]:
    """產生 certifi + 自備中繼憑證的合併 bundle，並指向它。回傳 bundle 路徑。"""
    global _installed_path
    if _installed_path:
        return _installed_path

    if all(os.environ.get(v) for v in _ENV_VARS):
        return None

    try:
        import certifi
    except ImportError:
        logger.warning("找不到 certifi，略過 CA bundle 合併（櫃買可能會憑證驗證失敗）")
        return None

    extras = sorted(_EXTRA_CERTS_DIR.glob("*.pem"))
    if not extras:
        return None

    try:
        parts = [Path(certifi.where()).read_text(encoding="utf-8")]
        parts += [p.read_text(encoding="utf-8") for p in extras]

        # delete=False + atexit：bundle 要活到 process 結束（每次連線都會重讀檔案）。
        fd, path = tempfile.mkstemp(prefix="finance_tools_ca_", suffix=".pem")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(parts))
    except Exception as e:
        logger.warning(f"合併 CA bundle 失敗，沿用系統預設: {e}")
        return None

    atexit.register(lambda: Path(path).unlink(missing_ok=True))

    for var in _ENV_VARS:
        os.environ.setdefault(var, path)

    _patch_default_context(path)

    _installed_path = path
    return path


def _patch_default_context(bundle: str) -> None:
    """讓 `ssl.create_default_context()`（urllib／http.client 用的那條）也吃到 bundle。

    只在呼叫端**沒有**自己指定憑證來源時才補；有指定就完全不動，不搶人家的信任設定。
    """
    original = ssl.create_default_context
    if getattr(original, "_finance_tools_patched", False):
        return

    def create_default_context(*args, **kwargs):
        ctx = original(*args, **kwargs)
        if not any(kwargs.get(k) for k in ("cafile", "capath", "cadata")):
            try:
                ctx.load_verify_locations(cafile=bundle)
            except Exception as e:  # bundle 壞掉也不該讓整個程式連不出去
                logger.warning(f"載入自備中繼憑證失敗: {e}")
        return ctx

    create_default_context._finance_tools_patched = True
    ssl.create_default_context = create_default_context

    # `ssl._create_default_https_context` 在 import ssl 當下就綁定了原函式，urllib 走的是它。
    # 只換 create_default_context 的話 urllib 完全不受影響（實測仍舊驗證失敗），必須一起換。
    if getattr(ssl, "_create_default_https_context", None) is original:
        ssl._create_default_https_context = create_default_context
