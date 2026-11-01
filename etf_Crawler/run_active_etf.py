#!/usr/bin/env python3
"""
主動型 ETF 持股：分層補齊的統籌程式（etf-active-daily.yml 唯一的抓取步驟）

每一層只處理「還沒拿到應有日期」的 ETF，已完成的直接跳過：

  1. 官網爬蟲（各投信一支，見 OFFICIAL）
  2. CMoney（網頁訪客 token；兆豐官網在 GitHub Actions 會 403，平常靠這層補）
  3. FinMind TaiwanStockActiveETFHolding（ETF_FINMIND_FALLBACK=true 才啟用，
     2026-10-01 確認 Sponsor 條款允許對外呈現後開啟）

一天三班（16:00／17:55／21:00）本身就是重試：前一班沒補到的，下一班從頭再走一次。
最後一班（台北 20 點以後）走完仍有缺，寄錯誤信。

「應有日期」：
  - 一般：最近一個台股交易日（台北 15 點前執行則取前一交易日）
  - LAGGED 內的海外型：官網固定晚一天發布，應有日期＝再前一個交易日

用法：
  uv run python etf_Crawler/run_active_etf.py                 # 全部分層跑一輪
  uv run python etf_Crawler/run_active_etf.py 00981A 00996A   # 只處理指定代號
  uv run python etf_Crawler/run_active_etf.py --status        # 只印狀態，不抓
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from etf_utils import prev_trading_day, today_tw, _tw_holidays  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
ETF_DIR = REPO_ROOT / "src/data/etf"
CRAWLER_DIR = Path(__file__).parent

# 投信 → (爬蟲腳本, 涵蓋代號)。新增主動型 ETF 時：有官網爬蟲就加進對應那一列，
# 沒有也會被 CMoney 層接住（清單讀 index.json）。
OFFICIAL = [
    ("統一",   "fetch_active_etf_unipres.py",         ["00403A", "00981A", "00988A"]),
    ("群益",   "fetch_active_etf_capital.py",         ["00982A", "00992A", "00997A"]),
    ("野村",   "fetch_active_etf_nomura.py",          ["00980A", "00985A", "00999A"]),
    ("元大",   "fetch_active_etf_yuanta.py",          ["00990A"]),
    ("中信",   "fetch_active_etf_ctbc.py",            ["00406A", "00983A", "00995A"]),
    ("復華",   "fetch_active_etf_fuhwa.py",           ["00991A", "00998A"]),
    ("安聯",   "fetch_active_etf_allianz.py",         ["00984A", "00993A"]),
    ("台新",   "fetch_active_etf_taishin.py",         ["00986A", "00987A"]),
    ("第一金", "fetch_active_etf_first_financial.py", ["00994A", "00408A"]),
    ("兆豐",   "fetch_active_etf_mega.py",            ["00996A"]),
    ("國泰",   "fetch_active_etf_cathay.py",          ["00400A"]),
    ("富邦",   "fetch_active_etf_fubon.py",           ["00405A"]),
    ("凱基",   "fetch_active_etf_kgi.py",             ["00407A"]),
    ("永豐",   "fetch_active_etf_sinopac.py",         ["00410A"]),
    ("聯博",   "fetch_active_etf_allianceb.py",       ["00404A"]),
    ("摩根",   "fetch_active_etf_jpmorgan.py",        ["00401A", "00989A"]),
]

# 官網固定晚一個交易日發布的（2026-10-01 21:30 實測：其餘 25 檔都已是 10-01，這 4 檔是 09-30）。
# 若哪檔被誤列／漏列，錯誤信會每天提醒，再回來調這裡。
LAGGED = {"00983A", "00988A", "00989A", "00998A"}

STEP_TIMEOUT = 15 * 60


def _taipei_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=8)


def latest_trading_day() -> str:
    """應有資料的最近交易日：台北 15 點前今天的持股還沒發布，取前一交易日。"""
    now = _taipei_now()
    d = today_tw()
    holidays = _tw_holidays()
    is_td = datetime.fromisoformat(d).weekday() < 5 and d not in holidays
    if is_td and now.hour >= 15:
        return d
    return prev_trading_day(d)


def expected_date(code: str, last_td: str) -> str:
    return prev_trading_day(last_td) if code in LAGGED else last_td


def active_codes() -> list:
    """主動型清單以 index.json 為準（與 CMoney 層一致：代號結尾 A）。"""
    for p in (REPO_ROOT.parent / "stock_map/src/data/etf/index.json", ETF_DIR / "index.json"):
        if p.exists():
            return sorted(e["code"] for e in json.load(open(p, encoding="utf-8")) if e["code"].endswith("A"))
    return sorted(p.stem for p in ETF_DIR.glob("*A.json"))


def current_date(code: str):
    p = ETF_DIR / f"{code}.json"
    if not p.exists():
        return None
    try:
        return json.load(open(p, encoding="utf-8")).get("lastUpdated")
    except Exception:                                          # noqa: BLE001
        return None


def pending(codes: list, last_td: str) -> list:
    return [c for c in codes if (current_date(c) or "") < expected_date(c, last_td)]


def run(script: str, codes: list) -> int:
    cmd = [sys.executable, str(CRAWLER_DIR / script), *codes]
    print(f"\n$ {script} {' '.join(codes)}", flush=True)
    try:
        return subprocess.run(cmd, cwd=REPO_ROOT, timeout=STEP_TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        print(f"  [ERROR] {script} 超過 {STEP_TIMEOUT // 60} 分鐘，中止")
        return 124


def write_output(key: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"{key}={value}\n")


def summary(codes: list, last_td: str, source: dict, official_failed: list) -> str:
    meta = {}
    for p in (REPO_ROOT.parent / "stock_map/src/data/etf/index.json", ETF_DIR / "index.json"):
        if p.exists():
            meta = {e["code"]: e for e in json.load(open(p, encoding="utf-8"))}
            break
    lines = [f"## 主動型 ETF 持股（最近交易日 {last_td}）", ""]
    still = pending(codes, last_td)
    lines.append(f"**{len(codes) - len(still)}/{len(codes)} 檔已到應有日期**"
                 + (f"；仍缺：{', '.join(still)}" if still else ""))
    if official_failed:
        lines.append(f"\n官網爬蟲失敗（已由後續層補上的不影響資料）：{'、'.join(official_failed)}")
    lines += ["", "| ETF | 名稱 | 投信 | 資料日期 | 應有 | 本班來源 |", "|---|---|---|---|---|---|"]
    for c in sorted(codes, key=lambda c: (c not in still, c)):
        m = meta.get(c, {})
        cur, exp = current_date(c) or "—", expected_date(c, last_td)
        mark = " ❌" if c in still else ""
        lines.append(f"| {c} | {m.get('name', '')} | {m.get('issuer', '')} | {cur}{mark} | {exp} | {source.get(c, '—')} |")
    return "\n".join(lines)


def main():
    args = sys.argv[1:]
    status_only = "--status" in args
    targets = [a for a in args if not a.startswith("--")]

    codes = targets or active_codes()
    last_td = latest_trading_day()
    source = {}            # 本班由哪一層補到
    official_failed = []

    todo = pending(codes, last_td)
    print(f"最近交易日 {last_td}；{len(codes)} 檔中待補 {len(todo)} 檔：{' '.join(todo) or '（無）'}")

    if not status_only and todo:
        # ── 第 1 層：官網 ──
        for issuer, script, covered in OFFICIAL:
            need = [c for c in covered if c in pending(codes, last_td)]
            if not need:
                continue
            before = {c: current_date(c) for c in need}
            rc = run(script, need)
            for c in need:
                if current_date(c) != before[c]:
                    source[c] = f"官網（{issuer}）"
            if rc != 0:
                official_failed.append(issuer)

        # ── 第 2 層：CMoney ──
        need = pending(codes, last_td)
        if need:
            before = {c: current_date(c) for c in need}
            run("fetch_active_etf_cmoney.py", need)
            for c in need:
                if current_date(c) != before[c]:
                    source[c] = "CMoney"

        # ── 第 3 層：FinMind（repo Variable 開關，可在故障時關閉）──
        need = pending(codes, last_td)
        if need and os.environ.get("ETF_FINMIND_FALLBACK", "").lower() == "true":
            before = {c: current_date(c) for c in need}
            run("fetch_active_etf_finmind.py", need)
            for c in need:
                if current_date(c) != before[c]:
                    source[c] = "FinMind"

    still = pending(codes, last_td)
    text = summary(codes, last_td, source, official_failed)
    print("\n" + text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text + "\n")
    write_output("missing", " ".join(still))
    write_output("official_failed", " ".join(official_failed))

    # 最後一班（台北 20 點後）仍有缺 → 寄信。前兩班有缺是正常的（還有下一班會補）。
    if still and not status_only and (_taipei_now().hour >= 20 or os.environ.get("ETF_ALERT_FORCE") == "true"):
        from notify_mail import send_mail
        rows = "".join(
            f"<tr><td style='padding:4px 10px;border:1px solid #e2e8f0'>{c}</td>"
            f"<td style='padding:4px 10px;border:1px solid #e2e8f0'>{current_date(c) or '—'}</td>"
            f"<td style='padding:4px 10px;border:1px solid #e2e8f0'>{expected_date(c, last_td)}</td></tr>"
            for c in still)
        run_url = ""
        if os.environ.get("GITHUB_RUN_ID"):
            run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                       f"{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
        html = (
            f"<p>今天最後一班跑完（官網 → CMoney{' → FinMind' if os.environ.get('ETF_FINMIND_FALLBACK') == 'true' else ''}），"
            f"以下主動型 ETF 仍未拿到應有日期的持股：</p>"
            f"<table style='border-collapse:collapse;font-size:13px'><tr>"
            f"<th style='padding:4px 10px;border:1px solid #e2e8f0'>ETF</th>"
            f"<th style='padding:4px 10px;border:1px solid #e2e8f0'>目前資料日</th>"
            f"<th style='padding:4px 10px;border:1px solid #e2e8f0'>應有</th></tr>{rows}</table>"
            + (f"<p>官網爬蟲失敗：{'、'.join(official_failed)}</p>" if official_failed else "")
            + (f"<p><a href='{run_url}'>這一班的 log</a></p>" if run_url else "")
        )
        send_mail(f"🚨 主動式 ETF 持股未補齊：{len(still)} 檔（{today_tw()}）", html)

    # 只有最後一班仍有缺才算失敗：16:00 有缺是常態（官網還沒發布），下一班會補。
    # 官網壞了但後面的層補上，資料沒問題，也不讓燈變紅（Job Summary 仍會列出）。
    final = _taipei_now().hour >= 20 or os.environ.get("ETF_ALERT_FORCE") == "true"
    sys.exit(1 if still and final and not status_only else 0)


if __name__ == "__main__":
    main()
