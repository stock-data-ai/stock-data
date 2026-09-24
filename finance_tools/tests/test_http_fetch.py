"""http_fetch 續傳：模擬櫃買「傳到一半就斷」（離線，假的 urlopen）。"""
import http.client
import json
import urllib.error

import pytest

from finance_tools.utils import http_fetch

PAYLOAD = json.dumps([{"code": f"{i:04d}", "v": "x" * 50} for i in range(3000)]).encode()


class _Resp:
    """送到 cut_at bytes 就斷。reset=True 模擬連線重置（例外裡**不帶**已收資料）。"""

    def __init__(self, status, body, headers, cut_at, reset=False):
        self.status, self._body, self.headers, self._cut, self._reset = status, body, headers, cut_at, reset
        self._pos = 0

    def read(self, amt=None):
        limit = len(self._body) if self._cut is None else min(self._cut, len(self._body))
        if self._pos >= limit:
            if limit < len(self._body):
                if self._reset:
                    raise ConnectionResetError(54, "Connection reset by peer")
                raise http.client.IncompleteRead(b"", len(self._body) - limit)
            return b""
        end = limit if amt is None else min(self._pos + amt, limit)
        chunk, self._pos = self._body[self._pos:end], end
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_server(monkeypatch, cut_at=None, honor_range=True, fail_before_data=None, reset=False):
    """每次回應最多給 cut_at bytes 就斷；Content-Length 照實宣告。回傳每次請求帶的 Range。"""
    calls = []

    def urlopen(req, timeout=None):
        rng = req.get_header("Range")
        calls.append(rng)
        if fail_before_data is not None and len(calls) in fail_before_data:
            raise urllib.error.URLError("connection reset")
        if rng and honor_range:
            start = int(rng.split("=")[1].rstrip("-"))
            body = PAYLOAD[start:]
            headers = {"Content-Length": str(len(body)),
                       "Content-Range": f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"}
            return _Resp(206, body, headers, cut_at, reset)
        return _Resp(200, PAYLOAD, {"Content-Length": str(len(PAYLOAD))}, cut_at, reset)

    monkeypatch.setattr(http_fetch.urllib.request, "urlopen", urlopen)
    return calls


def test_no_truncation_is_a_single_plain_request(monkeypatch):
    calls = _fake_server(monkeypatch)
    assert http_fetch.get_bytes("https://x/y") == PAYLOAD
    assert calls == [None]


def test_truncated_every_time_resumes_from_each_breakpoint(monkeypatch):
    calls = _fake_server(monkeypatch, cut_at=16_000)
    assert http_fetch.get_json("https://x/y") == json.loads(PAYLOAD)
    assert calls[0] is None
    assert calls[1] == "bytes=16000-"
    assert calls[2] == "bytes=32000-"
    assert len(calls) == -(-len(PAYLOAD) // 16_000)


def test_connection_drop_after_partial_data_resumes(monkeypatch):
    # 第 2 次請求（續傳那次）直接斷線：已收的不能丟，再接一次
    calls = _fake_server(monkeypatch, cut_at=100_000, fail_before_data={2})
    assert http_fetch.get_bytes("https://x/y") == PAYLOAD
    assert calls[1] == calls[2] == "bytes=100000-"


def test_failure_before_any_data_is_raised_to_caller(monkeypatch):
    # 一個 byte 都沒拿到不是續傳的問題：照常拋出，交給呼叫端既有的重試
    _fake_server(monkeypatch, fail_before_data={1})
    with pytest.raises(urllib.error.URLError):
        http_fetch.get_bytes("https://x/y")


def test_server_ignoring_range_never_splices_two_starts_together(monkeypatch):
    # 對方不理 Range、每次都從頭給又每次都斷：寧可放棄，也不能拼出壞資料
    _fake_server(monkeypatch, cut_at=16_000, honor_range=False)
    with pytest.raises(IOError):
        http_fetch.get_bytes("https://x/y")


def test_connection_reset_mid_body_keeps_what_was_received(monkeypatch):
    # 2026-09-13 實測的第二種斷法：讀到一半 ConnectionResetError，例外不帶已收資料。
    # 分段讀才保得住；一次 read() 整份的話第一段就整個丟掉，每次都從 0 開始、永遠收不完。
    calls = _fake_server(monkeypatch, cut_at=200_000, reset=True)
    assert http_fetch.get_bytes("https://x/y") == PAYLOAD
    assert calls[:2] == [None, "bytes=200000-"]
