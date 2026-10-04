"""
notify_mail.py — 以 AWS SES v2 寄信（純標準庫，與 health-check.yml 同一組 secrets 與收件人）

環境變數：AWS_SES_ACCESS_KEY_ID、AWS_SES_SECRET_ACCESS_KEY、AWS_SES_REGION
沒有憑證時只印訊息不寄（本機測試不會誤寄）。
"""

import hashlib
import hmac
import json
import os
import urllib.request
from datetime import datetime, timezone

FROM = "noreply@aistockmap.com"
TO = ["rf9550106@gmail.com"]   # 與 health-check.yml 同一收件人


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def send_mail(subject: str, html_body: str) -> bool:
    key_id = os.environ.get("AWS_SES_ACCESS_KEY_ID", "")
    secret = os.environ.get("AWS_SES_SECRET_ACCESS_KEY", "")
    region = os.environ.get("AWS_SES_REGION") or "ap-northeast-1"
    if not key_id or not secret:
        print(f"[mail] 沒有 SES 憑證，不寄：{subject}")
        return False

    host = f"email.{region}.amazonaws.com"
    payload = json.dumps({
        "FromEmailAddress": FROM,
        "Destination": {"ToAddresses": TO},
        "Content": {"Simple": {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Html": {"Data": html_body, "Charset": "UTF-8"}},
        }},
    }).encode("utf-8")

    now = datetime.now(timezone.utc)
    amz_date, date_stamp = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    signed_headers = "content-type;host;x-amz-date"
    canonical = "\n".join([
        "POST", "/v2/email/outbound-emails", "",
        f"content-type:application/json\nhost:{host}\nx-amz-date:{amz_date}\n",
        signed_headers, hashlib.sha256(payload).hexdigest(),
    ])
    scope = f"{date_stamp}/{region}/ses/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _sign(("AWS4" + secret).encode("utf-8"), date_stamp)
    for part in (region, "ses", "aws4_request"):
        k = _sign(k, part)
    signature = hmac.new(k, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    req = urllib.request.Request(
        f"https://{host}/v2/email/outbound-emails", data=payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Amz-Date": amz_date,
            "Authorization": (f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, "
                              f"SignedHeaders={signed_headers}, Signature={signature}"),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"[mail] SES {resp.status}：{subject}")
            return True
    except Exception as e:                                     # noqa: BLE001
        print(f"[mail] 寄信失敗（{e}）：{subject}")
        return False
