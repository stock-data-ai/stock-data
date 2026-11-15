"""SES 寄信（SigV4）只有一份：Python 是 etf_Crawler/notify_mail.py（workflow 告警信也 import 它），
cron Worker（cron/src/index.ts，不能 import Python）自留一份。兩份與 stock_map 的 src/shared/lib/ses.mjs、
scripts/lib/send_alert_email.py 由 stock_map 的 test_methods/market-events/ses.test.mjs 以同一組固定輸入對帳。

以前 health-check.yml 內嵌一份完整的 SigV4，與 notify_mail.py 各自維護。
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "etf_Crawler"))
from notify_mail import ses_headers  # noqa: E402

# AWS 文件的範例金鑰（不是真的）；期望值與 stock_map ses.test.mjs 的 AUTHORIZATION 相同
BODY = ('{"FromEmailAddress":"noreply@aistockmap.com","Destination":{"ToAddresses":["rf9550106@gmail.com"]},'
        '"Content":{"Simple":{"Subject":{"Data":"測試主旨 ✅","Charset":"UTF-8"},'
        '"Body":{"Text":{"Data":"純文字\\n第二行","Charset":"UTF-8"}}}}}')
SIGNATURE = "8a8391e398cda0da25712e8754066572f3c222586c903fb5056a1aacbb4fd397"


def test_ses_headers_fixed_vector():
    now = datetime(2026, 11, 15, 3, 4, 5, 678000, tzinfo=timezone.utc)
    headers = ses_headers(BODY.encode("utf-8"), "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
                          "ap-northeast-1", now)
    assert headers == {
        "Content-Type": "application/json",
        "X-Amz-Date": "20261115T030405Z",
        "Authorization": ("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261115/ap-northeast-1/ses/aws4_request, "
                          f"SignedHeaders=content-type;host;x-amz-date, Signature={SIGNATURE}"),
    }


def test_no_hand_written_sigv4():
    allowed = {"etf_Crawler/notify_mail.py", "cron/src/index.ts", "finance_tools/tests/test_ses_mail.py"}
    skip_dirs = {".git", ".venv", "node_modules", "__pycache__", "src", ".wrangler"}
    exts = (".py", ".yml", ".yaml", ".ts", ".js", ".mjs", ".sh")
    offenders = []
    for root, dirs, files in os.walk(REPO):
        rel_root = Path(root).relative_to(REPO).as_posix()
        # src/ 是資料目錄（略過）；cron/src 是程式，要掃
        dirs[:] = [d for d in dirs if d not in skip_dirs or (d == "src" and rel_root == "cron")]
        for name in files:
            if not name.endswith(exts):
                continue
            rel = (Path(root) / name).relative_to(REPO).as_posix()
            if rel in allowed:
                continue
            text = (Path(root) / name).read_text(encoding="utf-8", errors="ignore")
            if "AWS4-HMAC-SHA256" in text:
                offenders.append(rel)
    assert (REPO / ".github/workflows/health-check.yml").exists()  # 確認有掃到 workflow 目錄
    assert offenders == [], f"SES 寄信改用 etf_Crawler/notify_mail.py 的 send_mail：{offenders}"
