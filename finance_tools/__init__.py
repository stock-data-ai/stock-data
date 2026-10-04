# 憑證鏈補丁要在**任何 HTTPS 連線之前**生效：它靠環境變數改 CA bundle，而 OpenSSL 與
# requests 都只在建立連線／context 當下讀一次。放在套件 __init__ 是唯一不會漏的位置。
# 理由與來龍去脈見 finance_tools/utils/tls.py。
from finance_tools.utils.tls import install_ca_bundle

install_ca_bundle()
