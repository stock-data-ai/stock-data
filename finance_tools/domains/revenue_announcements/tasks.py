"""月營收：每月 1～13 號比對觀測站彙總表記下公布日，15～20 號用開放資料核對並寄信。

產物：
- `src/data/market/revenue-announcements/{YYYY-MM}.json`（YYYY-MM 是營收所屬月份），
  給 stock_map 行事曆的「營收」分頁讀。
- 同一個數字也補進 `company-financials/{code}.json` 的 `monthlyRevenue`，公司頁、選股、
  AI 助理在公布當天就看得到，不用等週日的 financials-update（FinMind）。週日那班照跑，
  以同一個 (年,月) 覆蓋；2026-11-11 實測兩邊已有的 650 家數字完全相同。
單位一律是元（觀測站與開放資料是千元，進來就乘 1000），與 `monthlyRevenue` 一致。

公布日（`date`）怎麼來：觀測站沒有申報日欄位，只能看「第一次出現在表上」是哪一班。
- 晚班（台灣 12:00 以後）第一次看到 → 當天，`window: "day"`
- 早班（12:00 以前）第一次看到 → 前一天，`window: "overnight"`：前一晚晚班之後到今早之間申報，
  多數是前一晚收盤後的公告；不管哪種，都是在今天開盤前公布
- 月份檔第一次建立時已是 3 號以後（功能上線、漏跑多天）→ 已在表上的公司 `date: null`，
  `window: "backfill"`，不猜日期
- 月中核對才補進來的（觀測站表上沒有，例如金控、保險）→ `date: null`，`window: "official"`
"""
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import finance_tools.config as config
from finance_tools.core import FileManager, merge_policy
from finance_tools.domains.financials.calculator import FinancialCalculator
from finance_tools.core.timezone import now_tw
from finance_tools.domains.revenue_announcements import fetcher

logger = logging.getLogger(__name__)

NUM_FIELDS = ("revenue", "prevMonth", "lastYear", "mom", "yoy", "cumulative", "cumLastYear", "cumYoy")
# 已有超過這麼多家時，新抓到的總數掉到這個比例以下就當作來源異常、不寫檔
SHRINK_GUARD_MIN, SHRINK_GUARD_RATIO = 100, 0.8
BACKFILL_FROM_DAY = 3
# 開放資料到這天還沒換到目標月份，就寄信說「還沒能核對」
VERIFY_GIVE_UP_DAY = 20
# 開放資料有、觀測站彙總表沒有是常態的產業（金控、保險另有申報格式）
EXPECTED_OFFICIAL_ONLY_INDUSTRIES = ("金融保險業", "金融業")
# 數字差多少才算不一致（元）。兩邊都是千元整數乘 1000，差 1 千元就是真的不同
REVENUE_TOLERANCE = 1


def target_month(now: datetime) -> Tuple[int, int]:
    """月營收是「上個月」的：11 月 1～13 號抓的是 10 月營收。"""
    first = now.replace(day=1)
    prev = first - timedelta(days=1)
    return prev.year, prev.month


def month_path(year: int, month: int) -> Path:
    return Path(config.MARKET_DATA_DIR) / "revenue-announcements" / f"{year}-{month:02d}.json"


def load_month(path: Path) -> Optional[Dict]:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dump_month(data: Dict) -> str:
    """一家一行：每天都改的檔，逐行 diff 才看得出今天多了誰。"""
    head = {k: v for k, v in data.items() if k != "companies"}
    lines = [json.dumps(c, ensure_ascii=False, separators=(",", ":")) for c in data["companies"]]
    body = json.dumps(head, ensure_ascii=False, indent=2)[:-2]
    return body + ',\n  "companies": [\n    ' + ",\n    ".join(lines) + "\n  ]\n}\n"


def save_month(path: Path, data: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dump_month(data))
        os.chmod(tmp, 0o644)   # mkstemp 預設 600
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


INDEX_MONTHS = 13


def write_index(directory: Path) -> Dict:
    """index.json：每個月份每天公布幾家。行事曆的「全部」只需要這份（幾 KB），
    切到「營收」才下載那個月的完整檔（gzip 後約 150KB）。"""
    months = {}
    files = sorted(p for p in directory.glob("????-??.json"))[-INDEX_MONTHS:]
    for p in files:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        by_date: Dict[str, int] = {}
        for c in d["companies"]:
            if c.get("date"):
                by_date[c["date"]] = by_date.get(c["date"], 0) + 1
        v = d.get("verification") or {}
        months[d["month"]] = {
            "updatedAt": d.get("updatedAt"),
            "total": len(d["companies"]),
            "byDate": dict(sorted(by_date.items())),
            "verified": v.get("ok") if v.get("checkedAt") else None,
        }
    index = {"updatedAt": now_tw().isoformat(timespec="minutes"), "months": months}
    tmp = directory / ".index.json.tmp"
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, directory / "index.json")
    return index


def previous_high(code: str, year: int, month: int, fm: FileManager) -> Dict:
    """目標月份以前的最高月營收（元），供「創新高」判斷。沒有歷史就不給。"""
    data = fm.load_financial_data(code)
    if not data:
        return {}
    hist = [m for m in (data.get("historical", {}).get("monthlyRevenue") or [])
            if (m.get("year"), m.get("month")) < (year, month) and m.get("revenue")]
    if not hist:
        return {}
    return {"prevHigh": max(m["revenue"] for m in hist), "historyMonths": len(hist)}


def apply_to_financials(companies: List[Dict], year: int, month: int, fm: FileManager) -> Dict[str, int]:
    """把月份檔的營收補進財報檔的 monthlyRevenue。只動目標月份那一筆，數字相同就不寫檔。

    沒有財報檔的公司跳過（新公司由 financials-update 建檔，這裡不拿單月資料建新檔）。
    每一班都對整份月份檔跑一次：冪等，漏跑或寫失敗的下一班會自己補上。
    """
    stats = {"updated": 0, "same": 0, "noFile": 0, "failed": 0}
    key = lambda m: (m.get("year"), m.get("month"))  # noqa: E731
    for c in companies:
        if c.get("revenue") is None:
            continue
        data = fm.load_financial_data(c["code"])
        if not data:
            stats["noFile"] += 1
            continue
        hist = data.setdefault("historical", {})
        monthly = hist.get("monthlyRevenue") or []
        cur = next((m for m in monthly if key(m) == (year, month)), None)
        if cur is not None and cur.get("revenue") == c["revenue"]:
            stats["same"] += 1
            continue
        hist["monthlyRevenue"] = merge_policy.merge_by_key(
            monthly, [{"year": year, "month": month, "revenue": float(c["revenue"]), "yoy": None}],
            key=key, limit=merge_policy.MONTHLY_REVENUE_LIMIT)
        FinancialCalculator.fill_monthly_yoy(hist["monthlyRevenue"])
        if fm.save_financial_data(c["code"], data):
            stats["updated"] += 1
        else:
            stats["failed"] += 1
    return stats


def _apply_and_log(data: Dict, year: int, month: int, fm: FileManager) -> None:
    s = apply_to_financials(data["companies"], year, month, fm)
    logger.info(f"財報檔月營收：更新 {s['updated']} 家、已相同 {s['same']} 家、"
                f"沒有財報檔 {s['noFile']} 家" + (f"、寫檔失敗 {s['failed']} 家" if s["failed"] else ""))
    if s["failed"]:
        sys.exit(1)


def _same_numbers(a: Dict, b: Dict) -> bool:
    return all(a.get(k) == b.get(k) for k in NUM_FIELDS)


def merge_announcements(existing: Optional[Dict], fetched: Dict[str, Dict], now: datetime,
                        year: int, month: int, fm: Optional[FileManager]) -> Tuple[Dict, Dict]:
    """把這一班抓到的表併進月份檔。回傳 (新檔內容, 統計)。純函式（除了讀歷史高點）。"""
    seen_at = now.isoformat(timespec="minutes")
    if now.hour < 12:
        date, window = (now - timedelta(days=1)).strftime("%Y-%m-%d"), "overnight"
    else:
        date, window = now.strftime("%Y-%m-%d"), "day"
    backfill = existing is None and now.day >= BACKFILL_FROM_DAY

    data = existing or {"month": f"{year}-{month:02d}", "companies": []}
    by_code = {c["code"]: c for c in data["companies"]}
    stats = {"new": 0, "revised": 0, "total": 0}

    for market in fetcher.MARKETS:
        for row in fetched[market]["rows"]:
            stats["total"] += 1
            old = by_code.get(row["code"])
            if old is None:
                rec = dict(row)
                if backfill:
                    rec.update(date=None, window="backfill")
                else:
                    rec.update(date=date, window=window)
                rec["seenAt"] = seen_at
                if fm is not None:
                    rec.update(previous_high(row["code"], year, month, fm))
                by_code[row["code"]] = rec
                stats["new"] += 1
            elif not _same_numbers(old, row) or old.get("note") != row.get("note"):
                # 公司更正：數字以最新為準，公布日不動
                old.update({k: row[k] for k in (*NUM_FIELDS, "note", "name", "industry")})
                old["revisedAt"] = seen_at
                stats["revised"] += 1

    data["companies"] = sorted(by_code.values(), key=lambda c: c["code"])
    data["updatedAt"] = seen_at
    data["sources"] = {m: {"generatedAt": fetched[m]["generatedAt"]} for m in fetcher.MARKETS}
    return data, stats


def run_update_revenue_announcements(args) -> None:
    now = now_tw()
    year, month = target_month(now)
    path = month_path(year, month)
    existing = load_month(path)
    logger.info(f"目標：{year}-{month:02d} 營收（{path}）")

    try:
        fetched = fetcher.fetch_mops(year, month)
    except fetcher.SourceError as e:
        logger.error(f"❌ 觀測站抓取失敗，不寫檔：{e}")
        sys.exit(1)

    total = sum(len(fetched[m]["rows"]) for m in fetcher.MARKETS)
    before = len(existing["companies"]) if existing else 0
    mops_before = sum(1 for c in (existing or {}).get("companies", []) if c.get("window") != "official")
    if mops_before >= SHRINK_GUARD_MIN and total < mops_before * SHRINK_GUARD_RATIO:
        logger.error(f"❌ 觀測站只回 {total} 家，上一班有 {mops_before} 家，疑似來源異常，不寫檔")
        sys.exit(1)

    fm = FileManager()
    data, stats = merge_announcements(existing, fetched, now, year, month, fm)
    if existing is not None and stats["new"] == 0 and stats["revised"] == 0:
        logger.info(f"沒有新公布或更正（表上 {total} 家），不改檔")
    else:
        save_month(path, data)
        write_index(path.parent)
        logger.info(f"✅ 新公布 {stats['new']} 家、更正 {stats['revised']} 家；累計 {len(data['companies'])} 家（原 {before}）")
    # 月份檔沒變也照跑：上一班寫財報檔失敗或被週日 FinMind 蓋成別的數字時，這裡會補回來
    _apply_and_log(data, year, month, fm)


# ── 月中核對 ──────────────────────────────────────────────────────────────

def verify_against_official(data: Dict, official: Dict[str, Dict], now: datetime,
                            fm: Optional[FileManager], year: int, month: int) -> Dict:
    """以開放資料為準修正月份檔，回傳核對報告（也會寫進 data['verification']）。"""
    seen_at = now.isoformat(timespec="minutes")
    by_code = {c["code"]: c for c in data["companies"]}
    official_rows = {r["code"]: r for m in fetcher.MARKETS for r in official[m]["rows"]}
    corrected, added = [], []

    for code, off in official_rows.items():
        ours = by_code.get(code)
        if ours is None:
            rec = {**off, "note": None, "foreign": "-KY" in off["name"], "date": None,
                   "window": "official", "seenAt": seen_at}
            if fm is not None:
                rec.update(previous_high(code, year, month, fm))
            by_code[code] = rec
            added.append({"code": code, "name": off["name"], "industry": off["industry"]})
            continue
        a, b = ours.get("revenue"), off.get("revenue")
        if (a is None) != (b is None) or (a is not None and abs(a - b) > REVENUE_TOLERANCE):
            corrected.append({"code": code, "name": off["name"],
                              "ours": ours.get("revenue"), "official": off.get("revenue")})
        if not _same_numbers(ours, off):
            ours.update({k: off[k] for k in NUM_FIELDS})
            ours["revisedAt"] = seen_at

    not_in_official = [{"code": c["code"], "name": c["name"]} for code, c in by_code.items()
                       if code not in official_rows]
    expected_added = [a for a in added if a["industry"] in EXPECTED_OFFICIAL_ONLY_INDUSTRIES]
    unexpected_added = [a for a in added if a["industry"] not in EXPECTED_OFFICIAL_ONLY_INDUSTRIES]

    report = {
        "checkedAt": seen_at,
        "officialGeneratedAt": {m: official[m]["generatedAt"] for m in fetcher.MARKETS},
        "officialCount": len(official_rows),
        "matched": len(official_rows) - len(added) - len(corrected),
        "corrected": corrected,
        "addedExpected": expected_added,
        "addedUnexpected": unexpected_added,
        "notInOfficial": not_in_official,
    }
    report["ok"] = not corrected and not unexpected_added and not not_in_official
    data["companies"] = sorted(by_code.values(), key=lambda c: c["code"])
    data["verification"] = report
    data["updatedAt"] = seen_at
    return report


def _fmt_ntd(v: Optional[float]) -> str:
    return "—" if v is None else f"{v / 1e8:,.2f} 億"


def _list_html(title: str, items: List[Dict], limit: int = 50) -> str:
    if not items:
        return ""
    lis = "".join(f"<li>{i['code']} {i['name']}</li>" for i in items[:limit])
    more = f"<li>……另 {len(items) - limit} 家</li>" if len(items) > limit else ""
    return f"<h3>{title}（{len(items)} 家）</h3><ul>{lis}{more}</ul>"


def report_mail(report: Dict, year: int, month: int) -> Tuple[str, str]:
    label = f"{year - 1911} 年 {month} 月營收核對"
    if report["ok"]:
        subject = f"✅ {label}：全部相符（{report['matched']:,} 家）"
    else:
        bits = []
        if report["corrected"]:
            bits.append(f"{len(report['corrected'])} 家數字不符")
        if report["addedUnexpected"]:
            bits.append(f"{len(report['addedUnexpected'])} 家漏抓")
        if report["notInOfficial"]:
            bits.append(f"{len(report['notInOfficial'])} 家不在正式資料")
        subject = f"⚠️ {label}：" + "、".join(bits)

    rows = "".join(
        f"<tr><td>{c['code']} {c['name']}</td><td>{_fmt_ntd(c['ours'])}</td><td>{_fmt_ntd(c['official'])}</td></tr>"
        for c in report["corrected"][:50])
    corrected = (f"<h3>數字不符，已改成正式資料（{len(report['corrected'])} 家）</h3>"
                 f"<table border=1 cellpadding=4><tr><th>公司</th><th>觀測站抓到</th><th>正式資料</th></tr>{rows}</table>"
                 if report["corrected"] else "")
    gen = "、".join(f"{m} {d}" for m, d in report["officialGeneratedAt"].items())
    body = (
        f"<p>用證交所／櫃買開放資料（出表 {gen}）核對每日從觀測站抓到的月營收。</p>"
        f"<p>正式資料 {report['officialCount']:,} 家，相符 {report['matched']:,} 家。</p>"
        + corrected
        + _list_html("觀測站沒抓到、已從正式資料補入", report["addedUnexpected"])
        + _list_html("抓到了但正式資料沒有（保留，請確認）", report["notInOfficial"])
        + _list_html("金融保險業（觀測站彙總表本來就沒有，已補入，屬正常）", report["addedExpected"], limit=30)
        + "<p>以上不一致都已在月份檔修正，行事曆下次更新就會用正式數字。</p>"
    )
    return subject, body


def _send_mail(subject: str, body: str) -> bool:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "etf_Crawler"))
    from notify_mail import send_mail  # noqa: E402  與其他 workflow 共用同一支 SES 寄信
    return send_mail(subject, body)


def run_verify_revenue_announcements(args) -> None:
    now = now_tw()
    year, month = target_month(now)
    if now.day < 14:
        # 13 號以前月份還在申報中，「上個月」不是要核對的那個月
        logger.info("14 號以前不核對")
        return
    path = month_path(year, month)
    data = load_month(path)
    label = f"{year - 1911} 年 {month} 月營收核對"
    if data is None:
        msg = f"<p>找不到 {path}，這個月的每日抓取可能都沒跑成功。</p>"
        logger.error(f"❌ {path} 不存在")
        _send_mail(f"⚠️ {label}：沒有每日抓取的資料", msg)
        sys.exit(1)
    if data.get("verification", {}).get("checkedAt"):
        logger.info(f"已在 {data['verification']['checkedAt']} 核對過，跳過")
        return

    try:
        official = fetcher.fetch_open_data()
    except fetcher.SourceError as e:
        logger.error(f"❌ 開放資料抓取失敗：{e}")
        sys.exit(1)

    want = f"{year}-{month:02d}"
    months = {m: official[m]["month"] for m in fetcher.MARKETS}
    if any(v != want for v in months.values()):
        logger.info(f"開放資料還是 {months}，尚未換到 {want}")
        if now.day >= VERIFY_GIVE_UP_DAY:
            _send_mail(f"⚠️ {label}：開放資料到 {now.day} 號還沒更新",
                       f"<p>證交所／櫃買開放資料目前是 {months}，還不是 {want}，本月無法核對。</p>"
                       "<p>每日抓到的觀測站數字照常提供，沒有被改動。</p>")
            data["verification"] = {"checkedAt": now.isoformat(timespec="minutes"), "ok": None,
                                    "skipped": f"開放資料未更新：{months}"}
            save_month(path, data)
            write_index(path.parent)
        return

    fm = FileManager()
    report = verify_against_official(data, official, now, fm, year, month)
    save_month(path, data)
    write_index(path.parent)
    # 更正的數字與補入的金控、保險也寫進財報檔
    apply = apply_to_financials(data["companies"], year, month, fm)
    logger.info(f"財報檔月營收：更新 {apply['updated']} 家、已相同 {apply['same']} 家")
    logger.info(f"核對完成：相符 {report['matched']}、不符 {len(report['corrected'])}、"
                f"補入 {len(report['addedExpected']) + len(report['addedUnexpected'])}、"
                f"正式資料沒有 {len(report['notInOfficial'])}")
    subject, body = report_mail(report, year, month)
    if not _send_mail(subject, body):
        # 檔已寫好；寄信失敗讓 workflow 變紅，健康檢查看得到
        logger.error("❌ 核對結果信寄送失敗")
        sys.exit(3)


def run_revenue_monthly(args) -> None:
    """排程只派這一支，依日期決定做什麼：1～13 號收集公布、15～20 號月中核對、其他日子不做事。"""
    day = now_tw().day
    if day <= 13:
        run_update_revenue_announcements(args)
    elif 15 <= day <= VERIFY_GIVE_UP_DAY:
        run_verify_revenue_announcements(args)
    else:
        logger.info(f"{day} 號不在收集（1～13）或核對（15～{VERIFY_GIVE_UP_DAY}）期間，不做事")
