#!/usr/bin/env python3
"""處置股預測引擎 + Official Notice Reconciliation（P1 驗證框架）。

多款規則引擎：每檔命中股票輸出 `rules[]`，逐條列出「命中哪款/哪標準/實際值 vs 門檻/理由」。
款別實作狀態（見 docs/future/處置股預測.md §3）：
  款1 ✅  款2 ✅  款3 ✅  款4 ✅  款6 ⚠️(PE/PB;PB為openapi最新日)  款7 ⚠️(margin為最新日,forward準)
  款5 ❌ 分點資料不可得；款8 TDR 不做。
官方標籤（GT）：上市走 TWSE rwd 端點，吃 startDate/endDate **可回溯數年**且原文含「第X款」；
上櫃 rwd 端點同樣吃 startDate/endDate 可回溯（`date=` 無效，會回最新日——老坑）。

用法：
  python3 p1_reconcile.py predict [YYYYMMDD]   # 算某收盤日全款 prediction（省略=今天）
  python3 p1_reconcile.py notice               # 抓當前官方 notice/notetrans 快照（存原文）
  python3 p1_reconcile.py backfill-notice [S] [E]  # 回補官方注意名單（含款號）→ notices/official_*.json
  python3 p1_reconcile.py reconcile            # 逐款對帳 prediction vs 官方款號 → confusion matrix
  python3 p1_reconcile.py record               # 戰績：被處置的個股我們事前示警過幾檔
  python3 p1_reconcile.py explain CODE [DATE]  # 印某檔的完整命中理由
  python3 p1_reconcile.py daily                # predict(today)+notice 一次做

注意：注意名單的「日期」欄 = **資料日（觸發日）本身，不是隔日公告日**——實測 763/763 筆
其「收盤價」欄等於該日收盤、0 筆等於前一交易日 → reconcile 直接同日對帳，勿再 off-by-one。
"""
import json, os, sys, re, math, datetime, statistics
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE, PRED, NOTI = (os.path.join(HERE, d) for d in ("cache", "predictions", "notices"))
FCAST = os.path.join(HERE, "forecasts")        # 每日「真的發布出去」的預警快照（戰績唯一可信來源）
for _d in (CACHE, PRED, NOTI, FCAST):          # 可再生目錄在乾淨 checkout（CI）不存在，先建好
    os.makedirs(_d, exist_ok=True)
# 從美國 runner 連 TWSE 需帶 Referer（對齊 domains/margin_trading 等能成功的抓法），
# 否則 TWSE 容易把裸請求掛住 → timeout。用 session 共用連線 + tenacity 自動重試。
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.twse.com.tw/"}
_SESSION = requests.Session()
_SESSION.headers.update(_HEADERS)

# ── 各款門檻（market-specific，見文件 §3）──────────────────────
# k1.gap = 「25%版」起迄兩營業日收盤價價差門檻。2026-08-03 從官方標籤反推校準：
#   25%版命中中，官方也列的（TP）價差中位數 135 元、最小 48.5；官方沒列的（FP）中位數僅 15.8。
#   門檻掃描 25→70 元，**50 元**保住 79/81 TP 而把 FP 從 179 砍到 24（該版 P 31%→77%）。
#   → 文件舊結論「gap=0 即等效、50 元是誤植款11」作廢；50 元是款1 25%版的真門檻。
#   上櫃沿用 50（官方名單只累積 2 日，尚無法獨立校準，待資料足夠再驗）。
CFG = {
    "TWSE": {"k1": {"std1": 32.0, "std2": 25.0, "gap": 50.0},
             "k2": {30: (100.0, 85.0), 60: (130.0, 110.0), 90: (160.0, 135.0)},
             "k2_low": None,                       # 上市無低價股特別門檻
             "k4_turn": 10.0},
    "TPEx": {"k1": {"std1": 30.0, "std2": 27.0, "gap": 50.0},
             "k2": {30: (100.0, 80.0), 60: (130.0, 80.0), 90: (160.0, 80.0)},
             "k2_low": {30: 120.0, 60: 180.0, 90: 240.0},   # 收盤<5元 上櫃特別門檻
             "k4_turn": 5.0},
}

# ── 2026-08-10 新制（證交所/櫃買 2026-08 修正，自 2026/08/10 施行）───────────
#   一、處置期間：一般 10 → **5** 營業日；當沖占比過高加重者 12 → **7** 營業日。
#   二、撮合間隔：5分/20分 → 一律**約每 2 分鐘**（引擎不推測，只讀官方公告原文）。
#   三、高價股注意標準放寬：款1「低標版」（25%/27%＋起迄價差）改為**僅適用收盤價 >1000 元**，
#       且價差門檻 100 → 300 元；>2000 元後每增 1000 元一級距 +150 元（2000~3000 → 450）。
# 新制只對 as_of >= 施行日生效，舊資料仍走舊制，否則歷史準確度會被今天的規則污染。
RULE_2026_08_10 = "20260810"

def _gap_threshold(close, as_of, legacy):
    """款1「低標版」所需的 6 日起迄價差門檻；回 None＝該版本此時不適用（只剩高標 32%/30% 版）。"""
    if as_of < RULE_2026_08_10:
        return legacy                       # 舊制：不分價位一律 legacy（本引擎校準值 50 元）
    if close is None or close <= 1000:
        return None                         # 新制：低價股不再適用低標版
    return 300.0 + 150.0 * math.ceil(max(0.0, close - 2000.0) / 1000.0)

DIFF = 20.0                    # 款1 6日雙差幅門檻
PRICE_FLOOR = 5.0             # 收盤<5元 除外（款1，連帶其前置款3/4/7）
PE_LO, PE_HI = 0.0, 60.0     # 款1 類股比較豁免：PE<0 或 ≥60
MIN_PEERS = 5                # 款1 類股比較豁免：同類<5檔
# 款3（量能）
K3_VOLX, K3_XSPREAD = 5.0, 4.0            # 當日量/近60均量 ≥5；放大倍數 − 全市場 ≥4
K3_EX_TURN, K3_EX_VOL = 0.1, 500_000      # 除外：週轉<0.1% 或 量<500張(=50萬股)
# 款4（週轉率）
K4_TURN_SPREAD = 5.0                       # 週轉率 − 全市場平均 ≥5pp
# 款6（PE/PB）
K6_PE, K6_PB, K6_MULT = 60.0, 6.0, 2.0
K6_TURN, K6_VOL, K6_SECPB_MULT = 5.0, 3_000_000, 4.0   # 量≥3000張=300萬股
# 款7（券資比）
K7_RQ, K7_FIN, K7_SHORT, K7_RQ_X = 20.0, 25.0, 15.0, 4.0
# 款2 豁免
K2_EX_DISPOSED_RET = 10.0                  # 近60日曾處置 且 6日累積<10% → 豁免

def _f(x):
    try:
        return float(str(x).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None

@retry(stop=stop_after_attempt(5),
       wait=wait_exponential(multiplier=1, min=2, max=20),
       retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
       reraise=True)
def fj(url):
    r = _SESSION.get(url, timeout=25)
    r.raise_for_status()
    return r.json()

# ── 每日全個股資料（含量價/PE/發行股數）──────────────────────
def mi_index(date):
    """上市全個股 {code:{name,close,open,vol,amount,pe}}；快取。回 None=非交易日。"""
    cp = os.path.join(CACHE, f"mi_{date}.json")
    if os.path.exists(cp):
        x = json.load(open(cp))
    else:
        x = fj(f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={date}&type=ALLBUT0999")
        if x.get("stat") == "OK":
            json.dump(x, open(cp, "w"))
    if x.get("stat") != "OK" or "tables" not in x:
        return None
    tbl = next((t for t in x["tables"] if any("證券代號" in str(f) for f in t.get("fields", []))), None)
    out = {}
    for r in tbl["data"]:
        c = r[0]
        if len(c) == 4 and c[0].isdigit() and _f(r[8]) is not None:
            out[c] = {"name": r[1], "close": _f(r[8]), "open": _f(r[5]),
                      "vol": _f(r[2]), "amount": _f(r[4]),
                      "pe": _f(r[15]) if len(r) > 15 else None}
    return out

def tpex_daily(date):
    """上櫃全個股 {code:{name,close,open,vol,amount,shares}}；快取。回 None=非交易日。
    端點需西元斜線日期(YYYY/MM/DD)才回歷史；否則一律回最新日。"""
    cp = os.path.join(CACHE, f"otc_{date}.json")
    ymd_slash = f"{date[:4]}/{date[4:6]}/{date[6:8]}"
    if os.path.exists(cp):
        x = json.load(open(cp))
    else:
        x = fj(f"https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date={ymd_slash}&type=EW&response=json")
        if str(x.get("stat", "")).lower() == "ok" and str(x.get("date", "")) == date:
            json.dump(x, open(cp, "w"))
        elif str(x.get("date", "")) != date:
            return None
    if str(x.get("stat", "")).lower() != "ok" or "tables" not in x or str(x.get("date", "")) != date:
        return None
    tbl = next((t for t in x["tables"] if any("代號" in str(f) for f in t.get("fields", []))), None)
    if not tbl:
        return None
    out = {}
    for r in tbl["data"]:
        c = r[0]
        if len(c) == 4 and c[0].isdigit() and _f(r[2]) is not None:
            out[c] = {"name": r[1], "close": _f(r[2]), "open": _f(r[4]),
                      "vol": _f(r[8]), "amount": _f(r[9]),
                      "shares": _f(r[15]) if len(r) > 15 else None}
    return out

def pb_pe_daily(date):
    """上市 **當日** 本益比/股價淨值比（BWIBBU_d 可帶日期，與 openapi BWIBBU_ALL 的「最新日」不同）。
    款6 必須用當日值：openapi 版落後一日，實測 3450 聯鈞 08/03 官方 PB 11.42、openapi 給 10.39，
    門檻就卡在中間 → 款6 回測 0 命中。回 {code: {"pe","pb"}}；抓不到回 None 由呼叫端 fallback。"""
    cp = os.path.join(CACHE, f"bwibbu_{date}.json")
    if os.path.exists(cp):
        x = json.load(open(cp))
    else:
        try:
            x = fj(f"https://www.twse.com.tw/exchangeReport/BWIBBU_d?response=json&date={date}&selectType=ALL")
        except Exception:
            return None
        if x.get("stat") == "OK" and str(x.get("date", "")) == date:
            json.dump(x, open(cp, "w"))
    if x.get("stat") != "OK" or str(x.get("date", "")) != date:
        return None
    out = {}
    for r in x.get("data", []):
        if len(r) >= 7 and len(r[0]) == 4 and r[0][0].isdigit():
            out[r[0]] = {"pe": _f(r[5]), "pb": _f(r[6])}
    return out or None

def sector_pe():
    """{code:產業別}, {code:PE(openapi最新日)}。"""
    scp, pcp = os.path.join(CACHE, "sector.json"), os.path.join(CACHE, "pe.json")
    if os.path.exists(scp):
        sector = json.load(open(scp))
    else:
        sector = {r["公司代號"]: r.get("產業別", "") for r in fj("https://openapi.twse.com.tw/v1/opendata/t187ap03_L")}
        try:
            for r in fj("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"):
                sector.setdefault(r["SecuritiesCompanyCode"], "OTC" + str(r.get("SecuritiesIndustryCode", "")))
        except Exception:
            pass
        json.dump(sector, open(scp, "w"))
    pe = {}
    for r in fj("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"):
        pe[r["Code"]] = _f(r.get("PEratio"))
    json.dump(pe, open(pcp, "w"))
    return sector, pe

def ref_data():
    """發行股數(TWSE t187ap03_L)、PB、margin(券資比/使用率)。
    openapi 皆為最新日 → 回測近似、forward 準確（款6 PB、款7 margin）。"""
    shares = {}
    for r in fj("https://openapi.twse.com.tw/v1/opendata/t187ap03_L"):
        s = _f(r.get("已發行普通股數或TDR原股發行股數"))
        if s:
            shares[r["公司代號"]] = s
    pb = {}
    for r in fj("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"):
        pb[r["Code"]] = _f(r.get("PBratio"))
    margin = {}
    for r in fj("https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN"):
        fin, fin_lim = _f(r.get("融資今日餘額")), _f(r.get("融資限額"))
        sht, sht_lim = _f(r.get("融券今日餘額")), _f(r.get("融券限額"))
        if not fin or fin <= 0:
            continue
        margin[r["股票代號"]] = {
            "rq": (sht / fin * 100) if sht is not None else None,           # 券資比
            "fin_use": (fin / fin_lim * 100) if fin_lim else None,          # 融資使用率
            "sht_use": (sht / sht_lim * 100) if (sht is not None and sht_lim) else None,   # 融券使用率
        }
    return shares, pb, margin

def trading_days_back(date, n):
    """從 date 往前收集 n 個交易日（含 date），需能取得資料（快取或抓取）。"""
    out = []
    d = datetime.datetime.strptime(date, "%Y%m%d").date()
    for _ in range(n * 3):
        ymd = d.strftime("%Y%m%d")
        if mi_index(ymd):
            out.append(ymd)
        if len(out) >= n:
            break
        d -= datetime.timedelta(days=1)
    return list(reversed(out))

# ── 引擎輔助 ────────────────────────────────────────────────
def _series(loader, market_date, n):
    """回 [(date, data)] 由舊到新，最多 n 個交易日（含 market_date）。缺資料的日子略過。"""
    out = []
    d = datetime.datetime.strptime(market_date, "%Y%m%d").date()
    for _ in range(n * 3):
        ymd = d.strftime("%Y%m%d")
        try:
            data = loader(ymd)
        except Exception:
            data = None                  # 網路失敗當缺日跳過（不中斷整體）
        if data:
            out.append((ymd, data))
        if len(out) >= n:
            break
        d -= datetime.timedelta(days=1)
    return list(reversed(out))

def _ret(series, c, p):
    """c 的 p 營業日累積漲跌%（close[t]/close[t-(p-1)]-1）。資料不足回 None。"""
    if len(series) < p:
        return None
    old, new = series[-p][1].get(c), series[-1][1].get(c)
    if not old or not new or not old.get("close") or old["close"] <= 0:
        return None
    return (new["close"] - old["close"]) / old["close"] * 100

LIMIT_MOVE = 10.5      # 台股單日漲跌幅上限 10%（留 0.5 給 tick 誤差）

def _price_break(series, c, p):
    """c 在最近 p 個交易日窗內是否出現「非交易因素」跳空 → 該窗報酬率不可信。
    單日超過 ±10% 在台股不可能靠交易達成，必為減資/除權息/停牌復牌/新上市無漲跌幅。
    官方款1/款2 均明文排除非交易因素；不濾會產生假漲幅（實例：虹光 2380 減資停牌 8 日，
    復牌 6.60→21.50 = +226%，我們連喊 10 天「中長期漲太多」，官方一次都沒點名）。"""
    seq = [e[1][c]["close"] for e in series[-p:]
           if c in e[1] and e[1][c].get("close")]
    return any(seq[i - 1] and abs(seq[i] / seq[i - 1] - 1) * 100 > LIMIT_MOVE
               for i in range(1, len(seq)))

def self_k1_history(series, sector, cfg, lookback=30):
    """自算的「近 lookback 日曾符合款1(25%版)」代號集合 —— 官方名單不可回溯的市場（上櫃）
    用它當款2 豁免的 proxy。純用手上的 series 逐日重算，不讀 predictions/，故與執行順序無關。
    比官方標籤粗（省略 PE 豁免、用同類≥5 判定），但豁免本身只是「曾被點名過」的粗篩。"""
    out = set()
    std2 = cfg["k1"]["std2"]
    for t in range(len(series) - 1, max(len(series) - 1 - lookback, 6), -1):
        today = series[t][1]
        rets, by_sec = {}, {}
        for c, row in today.items():
            cl = row.get("close")
            if cl is None or cl < PRICE_FLOOR:
                continue
            if _price_break(series[:t + 1], c, 6):
                continue
            a = _ret_cum(series[:t + 1], c, 6)     # 同款1：逐日累加
            if a is None:
                continue
            rets[c] = a
            by_sec.setdefault(sector.get(c, "?"), []).append(rets[c])
        if not rets:
            continue
        mkt = statistics.mean(rets.values())
        peer = {s: statistics.mean(v) for s, v in by_sec.items()}
        pn = {s: len(v) for s, v in by_sec.items()}
        for c, a in rets.items():
            if abs(a) <= std2 or abs(a - mkt) < DIFF:
                continue
            s = sector.get(c, "?")
            if pn.get(s, 0) >= MIN_PEERS and abs(a - peer[s]) < DIFF:
                continue
            out.add(c)
    return out

def _ret_cum(series, c, n=6):
    """款1 官方度量：**逐日漲跌幅算術累加**（非頭尾相除）。
    2026-08-03 用官方原文自述的百分比大規模驗證：475 筆款1 中，逐日累加吻合 95.8%、
    端點法(close[t]/close[t-5]) 僅 1.9%、(t-6) 僅 0.2% → 文件 §4.1 的候選 A 作廢，改用 C。
    「最近六個營業日(含當日)」實測 = **6 個日變動**（7 個價格點）。"""
    seq = [e[1][c]["close"] for e in series if c in e[1] and e[1][c].get("close")]
    if len(seq) < n + 1:
        return None
    seq = seq[-(n + 1):]
    return sum((seq[i] / seq[i - 1] - 1) * 100 for i in range(1, len(seq)) if seq[i - 1])

DAY_LIMIT = 10.0       # 台股單日漲跌幅上限 → 明天的 6 日累積只能在 base5 ± 10% 之間移動

# 兩條路徑取「最容易觸發」者。同一 rank 內用 _need 比較（漲側直接比所需漲幅：hold 為負、
# rise 為正，天然排在前；跌側比所需跌幅的絕對值）。**不可用 abs(pct) 排序**——hold 的
# |pct| 越小代表門檻價越高、其實越難觸發，會挑到錯的那條。
_KIND_RANK = {"certain": 0, "hold": 1, "rise": 1, "fall": 2}

def _trigger_route(base5, close, start, base_th, gap_th, mkt6, peer6, peer_ok_flag):
    """單一觸發路徑（高標版無價差要求；低標版另需起迄價差 ≥ gap_th）→ 明天的觸發價。"""
    ups = [base_th, (mkt6 + DIFF) if mkt6 is not None else base_th]
    dns = [-base_th, (mkt6 - DIFF) if mkt6 is not None else -base_th]
    if peer_ok_flag and peer6 is not None:
        ups.append(peer6 + DIFF)
        dns.append(peer6 - DIFF)
    up, dn = max(ups), min(dns)
    need_up, need_dn = up - base5, dn - base5

    # 起迄價差要求換算成價位 → 再換成相對今日收盤的漲跌幅，與累積漲跌幅要求取較嚴者。
    if gap_th is not None:
        need_up = max(need_up, ((start + gap_th) / close - 1) * 100)
        need_dn = min(need_dn, ((start - gap_th) / close - 1) * 100)

    if need_up <= -DAY_LIMIT or need_dn >= DAY_LIMIT:
        # 緩衝＝跌停（或漲停）之後距門檻還剩幾個百分點。門檻含「大盤均值+20%」，
        # 大盤明天續漲會把門檻墊高 → 緩衝薄的不可講「一定」，前端據此降級為「很可能」。
        margin = (need_dn - DAY_LIMIT) if need_dn >= DAY_LIMIT else (-need_up - DAY_LIMIT)
        kind, pct, p, need = "certain", None, None, -margin
    elif need_up <= 0:
        kind, pct, p, need = "hold", need_up, close * (1 + need_up / 100), need_up
    elif need_up <= DAY_LIMIT:
        kind, pct, p, need = "rise", need_up, close * (1 + need_up / 100), need_up
    elif need_dn >= -DAY_LIMIT:
        kind, pct, p, need = "fall", need_dn, close * (1 + need_dn / 100), -need_dn
    else:
        return None                                    # 明天到不了，不輸出（免得洗版）
    out = {"kind": kind, "price": round(p, 2) if p is not None else None,
           "pct": round(pct, 2) if pct is not None else None, "_need": need,
           "base5": round(base5, 2), "threshold": round(up, 2), "gap_required": gap_th}
    if kind == "certain":
        out["margin"] = round(margin, 2)
    return out

def _trigger(series, c, close, cfg_k1, mkt6, peer6, peer_ok_flag, as_of):
    """反推「明天要到什麼價位才會觸發款1」。

    款1 度量是**逐日漲跌幅累加**，所以明天的 6 日累積 = 最近 5 個日變動（已知）+ 明天的日變動。
    門檻已知 → 明天所需漲跌幅可直接解出，觸發價 = 今日收盤 ×(1+所需%)。**盤後即可定案，不需即時報價。**

    因為單日漲跌上限 ±10%，會出現三種質性結論：
      base5 已 ≥ 門檻+10 → 明天跌停也躲不掉（certain）
      base5 已 ≥ 門檻     → 不跌破某價位就觸發（hold）
      否則              → 要漲/跌到某價位才觸發（rise/fall），差太遠則明天到不了（None）

    款1 有兩條路徑（高標版 32%/30%、低標版 25%/27%＋起迄價差），兩條都算再取**最易觸發**者。
    2026-08-10 起低標版只適用收盤價 >1000 元，一般股票只剩高標版 → 觸發價會自動變遠。

    門檻用「基準 vs 大盤均值±20%」取較嚴者——即文件 §4.2 說的「有效門檻」，僅供顯示。
    大盤均值盤中會動，故觸發價是**估計值**，不是保證。
    """
    seq = [e[1][c]["close"] for e in series if c in e[1] and e[1][c].get("close")]
    if len(seq) < 6 or close is None or close <= 0:
        return None
    seq = seq[-6:]                                     # 6 個價格點 = 明天窗內已知的 5 個日變動
    if any(x is None or x <= 0 for x in seq):
        return None
    base5 = sum((seq[i] / seq[i - 1] - 1) * 100 for i in range(1, 6))

    routes = [(cfg_k1["std1"], None)]
    gap_th = _gap_threshold(close, as_of, cfg_k1["gap"])
    if gap_th is not None:
        routes.append((cfg_k1["std2"], gap_th))
    # 明天那個 6 營業日窗的**起始日收盤** = seq[1]（明天入窗、最舊那天出窗）。
    # 不是 seq[0]——seq[0] 只參與累積漲跌幅的第一個日變動，價差窗比累積窗少一個點，見 _gap()。
    cands = [r for r in (_trigger_route(base5, close, seq[1], bt, gt, mkt6, peer6, peer_ok_flag)
                         for bt, gt in routes) if r]
    if not cands:
        return None
    best = min(cands, key=lambda x: (_KIND_RANK[x["kind"]], x["_need"]))
    return {k: v for k, v in best.items() if k != "_need"}

def _gap(series, c, n=5):
    """款1 低標版「起迄兩營業日收盤價價差」= 最近 6 個營業日的**頭尾**收盤價差（n=5 個間隔）。

    **與 _ret_cum 的窗不同，這是刻意的。** 累積漲跌幅是 6 個日變動（7 個價格點，含窗前一日
    到窗首日那一段，已由官方原文 475 筆實證）；價差則是那 6 個營業日自己的頭尾，只有 6 個點。
    2026-08-09 用 35 交易日重掃兩種窗：7 點窗在 50 元時上市 FP 34/FN 16，6 點窗同門檻
    FP 10/FN 14（F1 93.6 → 96.8）→ 6 點窗才是官方定義。
    """
    seq = [e[1][c]["close"] for e in series if c in e[1] and e[1][c].get("close")]
    return abs(seq[-1] - seq[-(n + 1)]) if len(seq) >= n + 1 else None

def _volavg(series, c, n):
    """c 近 n 營業日（不含當日）平均成交量；有效日 <20 回 None。"""
    vs = [e[1][c]["vol"] for e in series[-(n + 1):-1] if c in e[1] and e[1][c].get("vol")]
    return statistics.mean(vs) if len(vs) >= 20 else None

def _mean(vals):
    vals = [v for v in vals if v is not None]
    return statistics.mean(vals) if vals else None

def _cap_wavg(pairs):
    """cap 加權平均：pairs=[(value, cap)]。"""
    num = den = 0.0
    for v, w in pairs:
        if v is not None and w:
            num += v * w
            den += w
    return (num / den) if den else None


# ── 規則引擎（單一市場）──────────────────────────────────────
def rules_for_market(market, market_date, sector, pe_api, shares_tw, pb, margin,
                      official_hist, disposed):
    loader = mi_index if market == "TWSE" else tpex_daily
    cfg = CFG[market]
    # 款2 豁免用：近30日曾被官方公告款1 的代號（official_hist = official_index()）
    k1_recent = official_k1_recent(official_hist, market_date) if official_hist else set()
    series = _series(loader, market_date, 90)
    if not series or series[-1][0] != market_date:
        # 該市場當日尚未發布收盤（上櫃比上市晚），整個市場略過。回傳型別要與正常路徑一致，
        # 否則 predict 端的 tuple 拆解會拋錯、被 except 吞掉，變成「上櫃無聲消失」。
        print(f"[{market}] {market_date} 尚無收盤資料，略過該市場")
        return [], {}
    # 上櫃官方名單不可回溯（目前僅累積數日）→ 官方覆蓋不足時改用自算款1 歷史當 proxy，
    # 否則豁免形同失效、冷卻股舊帳會一路累積成假 danger（見文件 §0f）。
    cov, tot = official_coverage(market, market_date)
    if tot and cov < tot * 0.5:
        k1_recent |= self_k1_history(series, sector, cfg)
    # 款6 用「當日」PE/PB；上市有逐日端點，上櫃無 → 沿用 openapi 最新日（回測失真、forward 準）
    day_pb = pb_pe_daily(market_date) if market == "TWSE" else None
    today = series[-1][1]
    prev = series[-2][1] if len(series) >= 2 else {}

    # ── 每檔基礎 metrics ──
    M = {}
    for c, row in today.items():
        cl = row["close"]
        sh = row.get("shares") or shares_tw.get(c)          # 發行股數：OTC 直接有、TWSE 查 openapi
        turn = (row["vol"] / sh * 100) if (sh and row.get("vol")) else None
        va60 = _volavg(series, c, 60)
        M[c] = {
            "name": row["name"], "close": cl, "open": row.get("open"),
            "vol": row.get("vol"), "amount": row.get("amount"),
            "pe": ((day_pb or {}).get(c, {}).get("pe")
                   or (row.get("pe") if row.get("pe") is not None else pe_api.get(c))),
            "pb": (day_pb or {}).get(c, {}).get("pb") or pb.get(c),
            "shares": sh, "turnover": turn,
            "vol_ratio": (row["vol"] / va60) if (va60 and row.get("vol")) else None,
            "ret6": _ret_cum(series, c, 6),        # 款1：逐日累加（官方算法，見 _ret_cum）
            "ret30": _ret(series, c, 30), "ret60": _ret(series, c, 60), "ret90": _ret(series, c, 90),
            "sector": sector.get(c, "?"),
        }

    # ── 市場/同類 aggregates ──
    def agg(key):
        mkt = _mean([M[c][key] for c in M])
        grp = {}
        for c in M:
            if M[c][key] is not None:
                grp.setdefault(M[c]["sector"], []).append(M[c][key])
        peer = {s: statistics.mean(v) for s, v in grp.items()}
        peer_n = {s: len(v) for s, v in grp.items()}
        return mkt, peer, peer_n
    A6 = agg("ret6"); A30 = agg("ret30"); A60 = agg("ret60"); A90 = agg("ret90")
    Avr = agg("vol_ratio"); Atn = agg("turnover")
    # 款6 的 PE/PB 比較基準用**簡單平均**，不是市值加權。
    # 2026-08-03 以官方案例校準（雙鍵 4764 07/28，官方明列「PB 9.93 達類股四倍以上」）：
    #   同業PB 市值加權 2.66 → ×4=10.66 > 9.93 判不過；簡單平均 2.29 → ×4=9.17 < 9.93 判過 ✓
    #   全市場PB 市值加權 8.54（被台積電等權值股灌爆）→ ×2=17.09；簡單平均 2.62 → ×2=5.24 ✓
    mkt_pe_w = _mean([M[c]["pe"] for c in M])
    mkt_pb_w = _mean([M[c]["pb"] for c in M])
    sec_pb_w = {}
    for c in M:
        sec_pb_w.setdefault(M[c]["sector"], []).append(M[c]["pb"])
    sec_pb_w = {s: _mean(v) for s, v in sec_pb_w.items()}

    def peer_ok(s, peer_n, c):
        """款1類股比較是否適用：同類≥5 且 PE 非(<0或≥60)。"""
        p = M[c]["pe"]
        return peer_n.get(s, 0) >= MIN_PEERS and not (p is not None and (p < PE_LO or p >= PE_HI))

    hits, triggers = {}, {}
    def add(c, rule):
        hits.setdefault(c, {"code": c, "name": M[c]["name"], "market": market,
                            "close": M[c]["close"], "rules": []})["rules"].append(rule)

    for c in M:
        m = M[c]
        cl = m["close"]
        if cl is None or cl < PRICE_FLOOR:
            continue
        s = m["sector"]

        # ── 明天的觸發價（反推，盤後即可定案）──
        tg = _trigger(series, c, cl, cfg["k1"], A6[0],
                      A6[1].get(s), peer_ok(s, A6[2], c), market_date)
        if tg:
            triggers[c] = {"name": m["name"], "market": market, "close": cl, **tg}

        # ── 款1 ──（並算出 k1_25 供款3/4/7 前置）
        k1_25 = False
        if m["ret6"] is not None and not _price_break(series, c, 6):
            a, mkt6, peer6, pn6 = m["ret6"], A6[0], A6[1], A6[2]
            papp = peer_ok(s, pn6, c)
            mdiff = abs(a - mkt6) if mkt6 is not None else None
            pdiff = abs(a - peer6[s]) if (papp and s in peer6) else None
            m_ok = mdiff is not None and mdiff >= DIFF
            p_ok = (pdiff is not None and pdiff >= DIFF) if papp else True
            t = cfg["k1"]
            g = _gap(series, c, 5)          # 6 個營業日的頭尾價差，非 _ret_cum 的 7 點窗
            gap_th = _gap_threshold(cl, market_date, t["gap"])   # None＝低標版此時不適用
            lo_ok = gap_th is not None and abs(a) > t["std2"] and g is not None and g >= gap_th
            base_ok = abs(a) > t["std1"] or lo_ok
            if base_ok and m_ok and p_ok:
                std = "32%版" if abs(a) > t["std1"] else "25%版"
                add(c, {"clause": 1, "standard": std,
                        "metrics": {"return_6d": round(a, 2), "market_diff": round(mdiff, 2) if mdiff else None,
                                    "peer_diff": round(pdiff, 2) if pdiff is not None else None,
                                    "peer_applicable": papp,
                                    # 實際起迄價差留檔：門檻是逆推校準值，日後重掃門檻不必再全期重跑 predict
                                    "gap": round(g, 2) if g is not None else None},
                        "thresholds": {"return": t["std1"] if std == "32%版" else t["std2"], "diff": DIFF},
                        "reason": f"6日累積{a:+.1f}%(>{t['std2' if std=='25%版' else 'std1']}%) 且 與大盤差{mdiff:.1f}%"
                                  + (f"、與同類差{pdiff:.1f}%(均≥20%)" if papp and pdiff is not None else "、同類比較豁免")})
            # k1_25：符合款1「25%版」門檻（供款3/4/7 前置）。
            # 刻意不套價差／2026-08 新制的高價股限制——這是對帳校準出來的寬鬆前置，
            # 綁上款1 的完整條件會連動改變款3/4/7 的命中率，需另行校準才能動。
            k1_25 = (abs(a) > t["std2"]) and m_ok and p_ok

        # ── 款2（30/60/90 中長期）── 需長期資料 + 方向 + 豁免
        for p in (30, 60, 90):
            r = m[f"ret{p}"]
            if r is None:
                continue
            ret_th, diff_th = cfg["k2"][p]
            if cfg["k2_low"] and cl < 5:            # 上櫃低價股特別門檻
                ret_th = cfg["k2_low"][p]
            if abs(r) <= ret_th:
                continue
            if _price_break(series, c, p):        # 非交易因素跳空 → 該期報酬不可信
                continue
            Ap = {30: A30, 60: A60, 90: A90}[p]
            mkt_p, peer_p, pn_p = Ap
            papp = pn_p.get(s, 0) >= MIN_PEERS
            mdiff = abs(r - mkt_p) if mkt_p is not None else None
            pdiff = abs(r - peer_p[s]) if (papp and s in peer_p) else None
            if not (mdiff is not None and mdiff >= diff_th):
                continue
            if papp and not (pdiff is not None and pdiff >= diff_th):
                continue
            # 方向：漲幅觸發須收紅（今收>昨收）、跌幅須收黑
            pc = prev.get(c, {}).get("close")
            if pc is not None:
                if r > 0 and not (cl > pc):
                    continue
                if r < 0 and not (cl < pc):
                    continue
            # 豁免1：近60日曾處置 且 6日累積<10%
            if c in disposed and m["ret6"] is not None and abs(m["ret6"]) < K2_EX_DISPOSED_RET:
                continue
            # 豁免2：近30日曾公告款1 且 6日累積未超過 25%(上市)/27%(上櫃) → 已冷卻，不再算款2
            # （官方用這條過濾「舊帳」；缺它會讓款2 對已冷卻股持續亂喊，實測 Precision 僅 15.9%）
            if c in k1_recent and m["ret6"] is not None and abs(m["ret6"]) <= cfg["k1"]["std2"]:
                continue
            add(c, {"clause": 2, "standard": f"{p}日",
                    "metrics": {f"return_{p}d": round(r, 2), "market_diff": round(mdiff, 2),
                                "peer_diff": round(pdiff, 2) if pdiff is not None else None},
                    "thresholds": {"return": ret_th, "diff": diff_th},
                    "reason": f"{p}日累積{r:+.1f}%(>{ret_th}%) 且 與大盤差{mdiff:.1f}%(≥{diff_th}%)"})

        # ── 款3（量能）── 前置：款1 25%版
        if k1_25 and m["vol_ratio"] is not None and Avr[0] is not None:
            vr, mkt_vr = m["vol_ratio"], Avr[0]
            ex = (m["turnover"] is not None and m["turnover"] < K3_EX_TURN) or (m["vol"] is not None and m["vol"] < K3_EX_VOL)
            if not ex and vr >= K3_VOLX and (vr - mkt_vr) >= K3_XSPREAD:
                add(c, {"clause": 3, "standard": "量能",
                        "metrics": {"vol_ratio": round(vr, 2), "market_vol_ratio": round(mkt_vr, 2),
                                    "spread": round(vr - mkt_vr, 2)},
                        "thresholds": {"vol_ratio": K3_VOLX, "spread": K3_XSPREAD},
                        "reason": f"當日量為近60日均量{vr:.1f}倍(≥5) 且 放大倍數比全市場多{vr-mkt_vr:.1f}倍(≥4)"})

        # ── 款4（週轉率）── 前置：款1 25%版
        if k1_25 and m["turnover"] is not None and Atn[0] is not None:
            tn, mkt_tn, th = m["turnover"], Atn[0], cfg["k4_turn"]
            if tn >= th and (tn - mkt_tn) >= K4_TURN_SPREAD:
                add(c, {"clause": 4, "standard": "週轉率",
                        "metrics": {"turnover": round(tn, 2), "market_turnover": round(mkt_tn, 2),
                                    "spread": round(tn - mkt_tn, 2)},
                        "thresholds": {"turnover": th, "spread": K4_TURN_SPREAD},
                        "reason": f"當日週轉率{tn:.1f}%(≥{th}%) 且 高於全市場{tn-mkt_tn:.1f}pp(≥5pp)"})

        # ── 款6（PE/PB + 量能）── 部分：只算「同產業PB×4」附加條件
        pe_bad = (m["pe"] is not None and m["pe"] < 0) or \
                 (m["pe"] is not None and m["pe"] >= K6_PE and mkt_pe_w and m["pe"] >= mkt_pe_w * K6_MULT)
        if pe_bad and m["pb"] is not None and m["pb"] >= K6_PB and mkt_pb_w and m["pb"] >= mkt_pb_w * K6_MULT \
           and m["turnover"] is not None and m["turnover"] >= K6_TURN and m["vol"] is not None and m["vol"] >= K6_VOL:
            sec_ok = sec_pb_w.get(s) and m["pb"] >= sec_pb_w[s] * K6_SECPB_MULT
            if sec_ok:                                     # 三選一：僅同產業PB×4 可自算
                add(c, {"clause": 6, "standard": "PE/PB+同業PB",
                        "metrics": {"pe": m["pe"], "pb": m["pb"], "market_pb_w": round(mkt_pb_w, 2),
                                    "sector_pb_w": round(sec_pb_w[s], 2), "turnover": round(m["turnover"], 2)},
                        "thresholds": {"pb": K6_PB, "sector_pb_mult": K6_SECPB_MULT},
                        "reason": f"PE={m['pe']}/PB={m['pb']}(≥6且≥市場2倍) 且 PB≥同業{K6_SECPB_MULT}倍 且 週轉{m['turnover']:.1f}%≥5、量≥3000張"})

        # ── 款7（券資比）── 前置：款1 25%版；margin 為最新日（forward 準）
        mg = margin.get(c)
        if k1_25 and mg and mg["rq"] is not None and mg["fin_use"] is not None and mg["sht_use"] is not None:
            if mg["rq"] >= K7_RQ and mg["fin_use"] >= K7_FIN and mg["sht_use"] >= K7_SHORT:
                add(c, {"clause": 7, "standard": "券資比",
                        "metrics": {"rq_ratio": round(mg["rq"], 2), "fin_use": round(mg["fin_use"], 2),
                                    "sht_use": round(mg["sht_use"], 2)},
                        "thresholds": {"rq": K7_RQ, "fin_use": K7_FIN, "sht_use": K7_SHORT},
                        "note": "margin為openapi最新日，回測近似；6日最低券資比放大4倍條件需歷史，暫略",
                        "reason": f"券資比{mg['rq']:.1f}%(≥20) 且 融資使用率{mg['fin_use']:.1f}%(≥25) 且 融券使用率{mg['sht_use']:.1f}%(≥15)"})

    return list(hits.values()), triggers


def predict(market_date):
    days = trading_days_back(market_date, 6)
    if len(days) < 6:
        print(f"[predict] {market_date} 交易日不足6天（需先回補快取）: {days}")
        return
    sector, pe_api = sector_pe()
    shares_tw, pb, margin = ref_data()
    official_hist = official_index()          # 官方注意名單（含款號）→ 款2 豁免
    disposed = disposed_set()
    hits, triggers = [], {}
    for mkt in ("TWSE", "TPEx"):
        try:
            h, t = rules_for_market(mkt, market_date, sector, pe_api, shares_tw, pb, margin, official_hist, disposed)
            hits += h
            triggers.update(t)
        except Exception as e:
            print(f"[predict] {mkt} 引擎錯誤: {e}")
    rec = {"market_date": market_date, "prediction_generated_at": _now(),
           "engine": "multi-clause-v2", "clauses": [1, 2, 3, 4, 6, 7],
           "markets": sorted({h["market"] for h in hits} | {t["market"] for t in triggers.values()}),
           "predicted_codes": [h["code"] for h in hits], "hits": hits, "triggers": triggers}
    if len(rec["markets"]) < 2:
        print(f"[predict] ⚠️ 只有 {rec['markets']} 有資料 —— 另一市場當日收盤可能尚未發布，"
              f"該市場個股會整批從預警名單消失")
    json.dump(rec, open(os.path.join(PRED, f"pred_{market_date}.json"), "w"), ensure_ascii=False, indent=2)
    by = {}
    for h in hits:
        for r in h["rules"]:
            by.setdefault(r["clause"], set()).add(h["code"])
    summary = " ".join(f"款{k}={len(by[k])}" for k in sorted(by))
    print(f"[predict] {market_date}: 命中 {len(hits)} 檔（{summary or '無'}）")


def backtest(n=30, end=None):
    """一次生成近 n 個交易日 prediction（refs 只抓一次，回測用）。"""
    end = end or _today()
    days = trading_days_back(end, n)
    sector, pe = sector_pe()
    shares, pb, margin = ref_data()
    hist, disp = official_index(), disposed_set()
    for d in days:
        hits, triggers = [], {}
        for mkt in ("TWSE", "TPEx"):
            try:
                h, t = rules_for_market(mkt, d, sector, pe, shares, pb, margin, hist, disp)
                hits += h
                triggers.update(t)
            except Exception as e:
                print(f"  {d} {mkt} err: {e}")
        rec = {"market_date": d, "prediction_generated_at": _now(),
               "engine": "multi-clause-v2", "clauses": [1, 2, 3, 4, 6, 7],
               "predicted_codes": [h["code"] for h in hits], "hits": hits, "triggers": triggers}
        json.dump(rec, open(os.path.join(PRED, f"pred_{d}.json"), "w"), ensure_ascii=False, indent=2)
    print(f"[backtest] 生成 {len(days)} 日 prediction: {days[0]}~{days[-1]}")

def explain(code, market_date=None):
    market_date = market_date or _latest_pred()
    p = json.load(open(os.path.join(PRED, f"pred_{market_date}.json")))
    h = next((x for x in p["hits"] if x["code"] == code), None)
    if not h:
        print(f"{code} 於 {market_date} 未命中任何款")
        return
    print(f"\n=== {code} {h['name']}（{h['market']}，收盤 {h['close']}）@ {market_date} ===")
    for r in h["rules"]:
        print(f"  ● 款{r['clause']}（{r['standard']}）：{r['reason']}")
        if r.get("note"):
            print(f"      ⚠️ {r['note']}")

def _latest_pred():
    ps = sorted(f[5:13] for f in os.listdir(PRED) if f.startswith("pred_"))
    return ps[-1] if ps else _today()


def notice():
    """抓當前官方 notice+notetrans 原文快照 + 更新處置名單。announcement_date = 今天。"""
    refresh_punish()
    ann = _today()
    raw = {}
    for ep in ("notice", "notetrans"):
        try:
            raw[ep] = fj(f"https://openapi.twse.com.tw/v1/announcement/{ep}")
        except Exception as e:
            raw[ep] = {"error": str(e)}
    codes = {}
    for r in raw.get("notice", []) or []:
        if isinstance(r, dict) and r.get("Code"):
            codes[r["Code"]] = {"name": r.get("Name"), "reason_raw": r.get("TradingInfoForAttention", ""),
                                "count": r.get("NumberOfAnnouncement"), "src": "notice"}
    counter = {}
    for r in raw.get("notetrans", []) or []:
        if isinstance(r, dict) and r.get("Code"):
            counter[r["Code"]] = {"name": r.get("Name"),
                                  "criteria_raw": r.get("RecentlyMetAttentionSecuritiesCriteria", "")}
    rec = {"official_announcement_date": ann, "captured_at": _now(),
           "official_codes": list(codes), "detail": codes,
           "counter_codes": list(counter), "counter_detail": counter, "raw": raw}
    json.dump(rec, open(os.path.join(NOTI, f"notice_{ann}.json"), "w"), ensure_ascii=False, indent=2)
    print(f"[notice] {ann}: 官方注意 {len(codes)} 檔 -> {list(codes)}")

def _prev_trading(ann_date):
    d = datetime.datetime.strptime(ann_date, "%Y%m%d").date() - datetime.timedelta(days=1)
    for _ in range(10):
        ymd = d.strftime("%Y%m%d")
        if os.path.exists(os.path.join(CACHE, f"mi_{ymd}.json")):
            return ymd
        d -= datetime.timedelta(days=1)
    return None

_ROC = re.compile(r"(\d{3})年(\d{1,2})月(\d{1,2})日")
def _roc_dates(text):
    ds = [f"{int(y)+1911:04d}{int(m):02d}{int(d):02d}" for y, m, d in _ROC.findall(text)]
    if len(ds) < 2:
        return ds
    lo, hi = min(ds), max(ds)
    return [d for d in sorted(set(list_cached_days())) if lo <= d <= hi]

def list_cached_days():
    return sorted(f[3:11] for f in os.listdir(CACHE) if f.startswith("mi_") and f.endswith(".json"))

def _is_stock(code):
    return len(code) == 4 and code[0].isdigit()

def official_k1_by_date():
    """從所有 notetrans 快照聚合官方款1（連續N次）→ {trigger_date: {code: name}}。已排除權證。"""
    out = {}
    for f in sorted(os.listdir(NOTI)):
        if not f.startswith("notice_"):
            continue
        noti = json.load(open(os.path.join(NOTI, f)))
        for c, v in noti.get("counter_detail", {}).items():
            if not _is_stock(c):
                continue
            for d in _roc_dates(v.get("criteria_raw", "")):
                out.setdefault(d, {})[c] = v.get("name")
    return out

# ── 官方注意名單「帶款號」封存（可回溯）───────────────────────
# openapi 的 notice/notetrans 只回最新日、且 notetrans 無款號；
# 但 TWSE rwd 端點吃 startDate/endDate 可回溯數年，且原文含「第X款」標籤。
# TPEx 對應端點的 date 參數無效（一律回最近兩個公告日）→ 上櫃仍只能 forward 累積。
_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
       "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12, "十三": 13, "十四": 14}
_CLAUSE_RE = re.compile(r"第([一二三四五六七八九十]+)款")

def _clauses(text):
    return sorted({_CN[c] for c in _CLAUSE_RE.findall(text or "") if c in _CN})

def _roc_to_ymd(s):
    """115.08.03 / 115/08/03 → 20260803。"""
    m = re.match(r"(\d{3})[./-](\d{1,2})[./-](\d{1,2})", str(s).strip())
    return f"{int(m.group(1))+1911:04d}{int(m.group(2)):02d}{int(m.group(3)):02d}" if m else None

def _official_rows_twse(start, end):
    x = fj("https://www.twse.com.tw/rwd/zh/announcement/notice"
           f"?startDate={start}&endDate={end}&response=json")
    if str(x.get("stat")) != "OK":
        raise RuntimeError(f"TWSE notice stat={x.get('stat')}")
    out = []
    for r in x.get("data") or []:
        ann = _roc_to_ymd(r[5])
        if ann:
            out.append({"date": ann, "code": r[1], "name": r[2], "market": "TWSE",
                        "count": _f(r[3]), "close": _f(r[6]), "text": r[4],
                        "clauses": _clauses(r[4])})
    return out

def _official_rows_tpex(start=None, end=None):
    """上櫃注意名單。**吃 startDate/endDate 可回溯**（實測一年前仍有資料）；
    `date=` 參數會被無視（一律回最近兩個公告日）——同 TPEx 歷史報價的老坑，別再踩。
    日期格式為西元斜線 YYYY/MM/DD。"""
    q = ""
    if start and end:
        q = (f"startDate={start[:4]}/{start[4:6]}/{start[6:8]}"
             f"&endDate={end[:4]}/{end[4:6]}/{end[6:8]}&")
    x = fj(f"https://www.tpex.org.tw/www/zh-tw/bulletin/attention?{q}response=json")
    tbl = next((t for t in x.get("tables", []) if t.get("data")), None)
    out = []
    for r in (tbl or {}).get("data", []):
        ann = _roc_to_ymd(r[5])
        if ann:
            out.append({"date": ann, "code": r[1], "name": r[2], "market": "TPEx",
                        "count": _f(r[3]), "close": _f(r[6]), "text": r[4],
                        "clauses": _clauses(r[4])})
    return out

def _save_official(rows):
    """依公告日分檔存 notices/official_{ann}.json；同市場覆寫、他市場保留（TWSE/TPEx 分開抓）。"""
    by = {}
    for r in rows:
        by.setdefault(r["date"], []).append(r)
    for ann, rs in by.items():
        fp = os.path.join(NOTI, f"official_{ann}.json")
        keep = []
        if os.path.exists(fp):
            markets = {r["market"] for r in rs}
            keep = [r for r in json.load(open(fp)).get("rows", []) if r.get("market") not in markets]
        json.dump({"date": ann, "captured_at": _now(),
                   "source": "twse-rwd/tpex-bulletin（原文含款號）", "rows": keep + rs},
                  open(fp, "w"), ensure_ascii=False, indent=2)
    return by

def backfill_notice(start=None, end=None):
    """回補官方注意名單（含款號）。省略日期 = 只抓上櫃最近兩日 + 上市近 60 天。"""
    end = end or _today()
    start = start or (datetime.datetime.strptime(end, "%Y%m%d")
                      - datetime.timedelta(days=60)).strftime("%Y%m%d")
    rows = []
    try:
        rows += _official_rows_twse(start, end)
    except Exception as e:
        print(f"[backfill] 上市抓取失敗: {e}")
    try:
        rows += _official_rows_tpex(start, end)
    except Exception as e:
        print(f"[backfill] 上櫃抓取失敗: {e}")
    by = _save_official(rows)
    print(f"[backfill] {start}~{end}: {len(rows)} 筆 / {len(by)} 個公告日 → notices/official_*.json")

def official_index():
    """{公告日: {代號: {"name","market","clauses":[...]}}}（排除權證）。"""
    out = {}
    for f in sorted(os.listdir(NOTI)):
        if not (f.startswith("official_") and f.endswith(".json")):
            continue
        rec = json.load(open(os.path.join(NOTI, f)))
        for r in rec.get("rows", []):
            if not _is_stock(r.get("code", "")):
                continue
            e = out.setdefault(rec["date"], {}).setdefault(
                r["code"], {"name": r.get("name"), "market": r.get("market"),
                            "close": r.get("close"), "clauses": set()})
            e["clauses"] |= set(r.get("clauses") or [])
    return out

def official_markets():
    """{公告日: {有抓到名單的市場}}。上櫃只有最近兩日 → 其餘日不可拿來算 FP。"""
    out = {}
    for f in sorted(os.listdir(NOTI)):
        if f.startswith("official_") and f.endswith(".json"):
            rec = json.load(open(os.path.join(NOTI, f)))
            out[rec["date"]] = {r.get("market") for r in rec.get("rows", [])}
    return out

def _trading_axis(idx=None):
    """交易日軸：mi_ 快取日 ∪ 官方公告日（兩者皆為交易日）。"""
    return sorted(set(list_cached_days()) | set(idx if idx is not None else official_index()))

def official_coverage(market, market_date, lookback=30):
    """近 lookback 個交易日內，該市場有官方名單的天數 / 總天數。"""
    avail = official_markets()
    axis = [d for d in _trading_axis() if d <= market_date][-lookback:]
    return sum(1 for d in axis if market in avail.get(d, set())), len(axis)

def official_k1_recent(idx, market_date, lookback=30):
    """近 lookback 個交易日內曾被官方公告款1 的代號（用於款2 豁免）。
    公告日 D 對應觸發日 D-1；30 日回看窗誤差一天不影響，直接用公告日軸。"""
    axis = [d for d in _trading_axis(idx) if d <= market_date][-lookback:]
    if not axis:
        return set()
    lo = axis[0]
    return {c for ann, m in idx.items() if lo <= ann <= market_date
            for c, v in m.items() if 1 in v["clauses"]}

# ── 官方處置名單 ────────────────────────────────────────────
# **不可用 openapi/announcement/punish**：它只回「已生效中」的處置，看不到「已公布、尚未生效」。
# 實例 2026-08-07：川湖等 5 檔當晚公布、08/10 生效，openapi 當晚查為空 → 我們誤判規則有錯。
# 改用 rwd/bulletin 端點：含公布日、可帶 startDate/endDate 回溯（實測拉得到兩個月以上），
# 且包含尚未生效的處置。日期參數篩的是**處置期間**，不是公布日 → 要往後查才看得到新公布的。
def _punish_rows_twse(start, end):
    x = fj("https://www.twse.com.tw/rwd/zh/announcement/punish"
           f"?startDate={start}&endDate={end}&response=json")
    if str(x.get("stat")) != "OK":
        raise RuntimeError(f"TWSE punish stat={x.get('stat')}")
    out = []
    for r in x.get("data") or []:
        st, en = _disp_period(r[6])
        streak, clause, times = _disp_reason(str(r[5]) + str(r[8]), str(r[7]))
        out.append({"code": str(r[2]), "market": "市", "name": str(r[3]),
                    "announced": _disp_roc(str(r[1])), "start": st, "end": en,
                    "interval": _disp_interval(str(r[8])),
                    "streak": streak, "clause": clause, "times": times})
    return out

def _punish_rows_tpex(start, end):
    x = fj("https://www.tpex.org.tw/www/zh-tw/bulletin/disposal"
           f"?startDate={start[:4]}/{start[4:6]}/{start[6:8]}"
           f"&endDate={end[:4]}/{end[4:6]}/{end[6:8]}&response=json")
    tbl = next((t for t in x.get("tables", []) if t.get("data")), None)
    out = []
    for r in (tbl or {}).get("data", []):
        st, en = _disp_period(str(r[5]))
        streak, clause, times = _disp_reason(str(r[6]) + str(r[7]), str(r[7]))
        out.append({"code": str(r[2]), "market": "櫃",
                    "name": re.sub(r"\(.*", "", str(r[3])).strip(),   # 名稱欄含 (../連結) 要去掉
                    "announced": _disp_roc(str(r[1])), "start": st, "end": en,
                    "interval": _disp_interval(str(r[7])),
                    "streak": streak, "clause": clause, "times": times})
    return out

def refresh_punish(days_back=90, days_fwd=30):
    """抓處置名單並存快取。往前抓 days_back 天（回補歷史）、往後 days_fwd 天（含未生效）。"""
    today = datetime.datetime.strptime(_today(), "%Y%m%d").date()
    start = (today - datetime.timedelta(days=days_back)).strftime("%Y%m%d")
    end = (today + datetime.timedelta(days=days_fwd)).strftime("%Y%m%d")
    rows = []
    for name, fn in (("上市", _punish_rows_twse), ("上櫃", _punish_rows_tpex)):
        try:
            rows += fn(start, end)
        except Exception as e:
            print(f"[punish] {name}抓取失敗: {e}")
    if rows:
        json.dump(rows, open(os.path.join(CACHE, "punish_rows.json"), "w"), ensure_ascii=False)
    return len(rows)

# 台股例假日以外的休市日（颱風停市等）。2026-08 無 → 空集合；跨到有休市日的期間要補。
_MARKET_HOLIDAYS = set()

def _bdays_from(start, n):
    """從 start（含）起算第 n 個營業日。未來日以週一~五近似（颱風停市無法預知）。"""
    d = datetime.datetime.strptime(start, "%Y%m%d").date()
    cnt = 0
    while True:
        ymd = d.strftime("%Y%m%d")
        if d.weekday() < 5 and ymd not in _MARKET_HOLIDAYS:
            cnt += 1
            if cnt >= n:
                return ymd
        d += datetime.timedelta(days=1)

def _bdays_span(start, end):
    """start~end 之間的營業日數（含頭尾）。"""
    d = datetime.datetime.strptime(start, "%Y%m%d").date()
    e = datetime.datetime.strptime(end, "%Y%m%d").date()
    n = 0
    while d <= e:
        if d.weekday() < 5 and d.strftime("%Y%m%d") not in _MARKET_HOLIDAYS:
            n += 1
        d += datetime.timedelta(days=1)
    return n

def _shorten_period(r, as_of):
    """2026-08-10 新制：處置期間 10→5 營業日（當沖加重 12→7）。

    官方**不會改寫已發布的舊制公告**，但實際上 8/10 起提前解除：至 8/10 前已處置滿
    5(7) 日者當日直接解禁，未滿者續到滿。不就地修正 end，畫面會把已出關的股票多關好幾天。

    只在 as_of >= 施行日才套用——8/10 之前那些股票是**真的還在處置中**，
    拿新制去改寫過去會把歷史對帳與戰績紀錄一起改掉。
    只動「舊制開始、跨越施行日」的那批；官方若自行改短，min() 會採用官方值。
    """
    st, en = r.get("start"), r.get("end")
    if as_of < RULE_2026_08_10 or not (st and en) or st >= RULE_2026_08_10 or en < RULE_2026_08_10:
        return r
    new_en = _bdays_from(st, 7 if _bdays_span(st, en) >= 12 else 5)   # ≥12 營業日＝當沖加重版
    return {**r, "end": min(en, new_en), "period_shortened": True} if new_en < en else r

def _punish_rows(as_of=None):
    fp = os.path.join(CACHE, "punish_rows.json")
    if not os.path.exists(fp):
        return []
    d = as_of or _today()
    try:
        return [_shorten_period(r, d) for r in json.load(open(fp)) if _is_stock(r.get("code", ""))]
    except (json.JSONDecodeError, OSError):
        return []

def disposed_set(as_of=None):
    """**處置生效中**的代號（start <= as_of <= end）。尚未生效的不算，那是 announced_set。"""
    d = as_of or _today()
    return {r["code"] for r in _punish_rows(d)
            if r.get("start") and r["start"] <= d and (not r.get("end") or d <= r["end"])}

def announced_set(as_of=None):
    """**已公布、尚未生效**的代號（start > as_of）——這批已達處置門檻，只是還沒開始。"""
    d = as_of or _today()
    return {r["code"] for r in _punish_rows(d) if r.get("start") and r["start"] > d}

# ── 處置公告細節（撮合/期間/原因/第幾次）——直接來自官方，非預測 ────────────
_CN = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
       '十': 10, '十一': 11, '十二': 12, '二十': 20}
def _cn2int(s):
    return _CN.get(s, int(s) if str(s).isdigit() else s)
def _disp_interval(t):
    m = re.search(r'每(?:約)?\s*([0-9]+|二十|十[一二三四五六七八九]?|[一二三四五六七八九十])\s*分鐘', t or '')
    return f"{_cn2int(m.group(1))}分" if m else None
def _disp_roc(s):
    s = re.sub(r'[^0-9]', '', s or '')
    return f"{int(s[:3]) + 1911}{s[3:5]}{s[5:7]}" if len(s) >= 7 else None
def _disp_period(s):
    s = (s or '').replace('～', '~').replace('－', '~').replace('至', '~')
    parts = [p for p in s.split('~') if p.strip()]
    return (_disp_roc(parts[0]), _disp_roc(parts[1])) if len(parts) >= 2 else (None, None)
def _disp_reason(text, measures=''):
    days = re.search(r'連續([一二三四五六七八九十0-9]+)個?營業日', text or '')
    streak = f"連續{_cn2int(days.group(1))}日" if days else None
    cm = re.search(r'第([一二三四五六七1-9])款', text or '')
    clause = f"第{_cn2int(cm.group(1))}款" if cm else ('第1款' if (days and _cn2int(days.group(1)) == 3) else None)
    tm = re.search(r'第([一二三四1-4])次', (measures or '') + (text or ''))
    times = f"第{_cn2int(tm.group(1))}次" if tm else None
    return streak, clause, times

def disposed_detail(as_of=None):
    """{code: {market,name,interval,start,end,streak,clause,times,announced,pending}}。
    pending=True 表示已公布但尚未生效（start > as_of）。同一檔多筆時取最新公布的那筆。"""
    d = as_of or _today()
    out = {}
    for r in sorted(_punish_rows(d), key=lambda x: (x.get("announced") or "", x.get("start") or "")):
        out[r["code"]] = {**r, "pending": bool(r.get("start") and r["start"] > d)}
    return out

def _k1_predicted(pred):
    """從 prediction 取出「命中款1」的代號集合（多款引擎後 reconcile 只比款1）。"""
    out = set()
    for h in pred.get("hits", []):
        if any(r["clause"] == 1 for r in h.get("rules", [])) and _is_stock(h["code"]):
            out.add(h["code"])
    return out

def _predicted(pred, clause, market):
    return {h["code"] for h in pred["hits"]
            if h.get("market") == market and _is_stock(h["code"])
            and any(r["clause"] == clause for r in h["rules"])}

def reconcile(show_fn=True):
    """逐款對帳：GT = 官方注意名單原文的「第X款」標籤（notices/official_*.json）。
    名單「日期」欄 = 資料日本身（已用收盤價欄實證 763/763）→ 與 prediction 同日直接對帳。"""
    preds = {}
    for f in os.listdir(PRED):
        if f.startswith("pred_"):
            p = json.load(open(os.path.join(PRED, f)))
            preds[p["market_date"]] = p
    idx = official_index()
    if not idx:
        print("[reconcile] 尚無 notices/official_*.json，先跑 `backfill-notice`")
        return
    avail = official_markets()
    stat = {}          # (market, clause) -> [TP, FP, FN]
    days = {}          # market -> 有 GT 的交易日數
    fn_rows, covered, unavail = [], [], []
    for mdate in sorted(preds):
        if mdate not in idx:
            unavail.append(mdate)
            continue
        covered.append(mdate)
        off = idx[mdate]
        for market in ("TWSE", "TPEx"):
            if market not in avail.get(mdate, set()):
                continue                      # 該市場當日無官方名單 → 不可當 GT（否則全成 FP）
            days[market] = days.get(market, 0) + 1
            for clause in (1, 2, 3, 4, 6, 7):
                pset = _predicted(preds[mdate], clause, market)
                oset = {c for c, v in off.items()
                        if clause in v["clauses"] and v.get("market") == market}
                s = stat.setdefault((market, clause), [0, 0, 0])
                s[0] += len(pset & oset); s[1] += len(pset - oset); s[2] += len(oset - pset)
                if clause == 1:
                    fn_rows += [(mdate, market, c, off[c]["name"]) for c in sorted(oset - pset)]

    print(f"\n=== Reconcile（GT=官方注意名單款號，排除權證）===")
    print(f"覆蓋 {len(covered)} 個交易日" + (f"（{covered[0]}~{covered[-1]}）" if covered else ""))
    for market in ("TWSE", "TPEx"):
        rows = [(c, stat[(market, c)]) for c in (1, 2, 3, 4, 6, 7) if (market, c) in stat]
        if not days.get(market):
            print(f"\n[{market}] 無官方標籤可對（上櫃端點不可回溯，需 forward 累積）")
            continue
        print(f"\n[{market}]（{days[market]} 個交易日有官方名單）"
              f" {'款':>3} {'TP':>4} {'FP':>4} {'FN':>4} {'Precision':>10} {'Recall':>8}")
        for c, (tp, fp, fn) in rows:
            p = f"{tp/(tp+fp):.1%}" if tp + fp else "—"
            r = f"{tp/(tp+fn):.1%}" if tp + fn else "—"
            print(f"      {c:>3} {tp:>4} {fp:>4} {fn:>4} {p:>10} {r:>8}")
    if unavail:
        print(f"\nGT unavailable（官方尚未涵蓋，不計入）: {len(unavail)} 日")
    if show_fn and fn_rows:
        print(f"\nFN 款1（官方有、我沒抓到 ← 金礦）共 {len(fn_rows)}，列最近 15 筆：")
        for d, mk, c, n in fn_rows[-15:]:
            print(f"  {d} [{mk}] {c} {n}")

# ── 戰績：我們當天真的示警過嗎 ─────────────────────────────
# 只認 forecasts/ 裡「當日實際發布」的快照。拿現在的引擎回頭重算過去等於用改良後的規則
# 考已知答案，不可宣稱。故沒有快照的日子一律留白，不補、不猜。
ALERT_LEVELS = {"danger", "near"}

def alert_history():
    """{as_of: {"published_at": ISO, "stocks": {code: {...}}}}"""
    out = {}
    for f in sorted(os.listdir(FCAST)):
        if f.startswith("forecast_") and f.endswith(".json"):
            rec = json.load(open(os.path.join(FCAST, f)))
            out[rec["as_of"]] = {"published_at": rec.get("published_at") or rec.get("generated_at"),
                                 "stocks": rec.get("stocks", {})}
    return out

ALERT_LOOKBACK = 10        # 最多往前追 10 個快照日：太久以前的示警不該算在這次處置頭上

def _alerted(hist, code, start):
    """該檔被處置前，我們最早從哪一個資料日起就把它列進警示。

    **只讀快照、不改快照**——快照記錄的是「那天使用者實際看到什麼」，補寫等於造假。
    往前追時這幾種日子**跳過**而非中斷：
      - 該檔當天不在名單上：達標後計數歸零曾被判成 safe 而遭裁掉（2026-08-07 川湖等 6 檔），
        那是顯示 bug，不是「沒示警」——08/06 的已發布名單裡它明明是 danger。
      - status 為 pending/disposed：那是結果，不是漏警。
    真正中斷的條件是「當天有列，但只到 watch/safe」——代表當時確實沒把它當回事。"""
    since = None
    seen_any = False
    for d in sorted((d for d in hist if d < start), reverse=True)[:ALERT_LOOKBACK]:
        rec = hist[d]
        # 只認「在處置生效前就已經發布出去」的名單——資料日可能是事後回補的，發布時間才算數
        pub = (rec.get("published_at") or "")[:10].replace("-", "")
        if pub and pub >= start:
            continue
        e = rec["stocks"].get(code)
        if not e:
            continue                      # 當天沒列 ≠ 沒示警（見 docstring）；繼續往前找
        seen_any = True
        if e.get("status") == "pending":
            continue                      # 達標當天是「結果」，不是漏警 → 跳過繼續往前找
        if e.get("status") == "disposed":
            if (e.get("next_countdown") or 99) <= 2:
                since = d
            continue                      # 已處置的日子不算漏警
        if e.get("status") in ALERT_LEVELS:
            since = d
            continue
        break
    if not since or not seen_any:
        return {}
    return {"alerted_since": since,
            "alerted_published_at": hist[since].get("published_at"),
            "alerted_level": hist[since]["stocks"][code]["status"]}

def save_forecast_snapshot(out):
    """把當日發布的預警名單存檔（進 git，跟 notices/ 同理：事後無法重建）。

    **抓資料失敗的爛結果不可入檔**——快照是戰績的唯一依據，存進一天「什麼都沒警告」
    會被永久當成漏警。判準同 CI 護欄：處置名單抓失敗（disposed=0）或全站零預警即視為異常。
    """
    fp_exist = os.path.join(FCAST, f"forecast_{out['as_of']}.json")
    if os.path.exists(fp_exist) and out["as_of"] < _today():
        # **既有的過去快照永不覆寫**：它記錄的是「那天實際發布給使用者的名單」，
        # 今天重跑會帶入今天的處置名單與改良後的規則，等於竄改歷史、讓戰績失真。
        print(f"[forecast] {out['as_of']} 快照已存在且非當日 → 保留原紀錄，不覆寫")
        return
    counts = out.get("counts", {})
    if not counts.get("disposed") or sum(counts.get(k, 0) for k in ("danger", "near", "watch")) == 0:
        print(f"[forecast] ⚠️ 結果異常（disposed={counts.get('disposed')} "
              f"warn={sum(counts.get(k, 0) for k in ('danger', 'near', 'watch'))}）→ 不存戰績快照")
        return
    fp = os.path.join(FCAST, f"forecast_{out['as_of']}.json")
    json.dump({"as_of": out["as_of"], "source": "engine", "generated_at": out.get("generated_at"),
               "stocks": {s["code"]: {"name": s.get("name"), "status": s["status"],
                                      "countdown": s.get("countdown"),
                                      **({"next_countdown": s["next_countdown"]}
                                         if s.get("next_countdown") is not None else {})}
                          for s in out.get("stocks", [])}},
              open(fp, "w"), ensure_ascii=False, indent=2)

DISPOSALS = os.path.join(FCAST, "disposals.json")   # 歷史處置事件（官方 API 只回進行中的，出關就消失）

def save_disposals():
    """把當前官方處置名單併入歷史檔（append-only，key=代號+開始日）。
    官方 punish/tpex_disposal API **只回進行中的**，處置期滿即消失 → 不存就永遠追不回，
    戰績也會跟著蒸發。每日 forecast 順手累積。"""
    try:
        hist = json.load(open(DISPOSALS)) if os.path.exists(DISPOSALS) else {}
    except (json.JSONDecodeError, OSError):
        hist = {}
    added = 0
    for c, v in disposed_detail().items():
        if not v.get("start"):
            continue
        k = f"{c}_{v['start']}"
        if k not in hist:
            hist[k] = {"code": c, **v, "first_seen": _today()}
            added += 1
    if added:
        json.dump(dict(sorted(hist.items())), open(DISPOSALS, "w"), ensure_ascii=False, indent=2)
    return added, len(hist)

def record():
    """戰績：被處置的個股中，我們事前示警過幾檔。
    只採 forecasts/ 的真實快照——沒有快照涵蓋的處置事件不計入分母（不是漏警，是當時還沒上線）。"""
    hist = alert_history()
    if not hist:
        print("[record] 尚無預警快照")
        return
    days = sorted(hist)
    try:
        disposals = json.load(open(DISPOSALS)) if os.path.exists(DISPOSALS) else {}
    except (json.JSONDecodeError, OSError):
        disposals = {}
    if not disposals:
        print("[record] 尚無處置事件歷史檔，先跑一次 forecast 累積")
        return

    # 涵蓋判定要用「發布時間」而非資料日：資料日 07/30 的名單若是 08/03 才發布，
    # 對 07/31 生效的處置根本來不及示警，算成漏警是自我抹黑。
    pubs = sorted(p for p in ((hist[d].get("published_at") or "") for d in days) if p)

    hit, miss, uncovered = [], [], 0
    for v in sorted(disposals.values(), key=lambda x: x.get("start") or ""):
        start = v.get("start")
        if not start or not any(p[:10].replace("-", "") < start for p in pubs):
            uncovered += 1                      # 處置生效前我們還沒發布過任何名單 → 不計分母
            continue
        a = _alerted(hist, v["code"], start)
        (hit if a.get("alerted_published_at") else miss).append((v, a))

    total = len(hit) + len(miss)
    print(f"\n=== 戰績（只計快照涵蓋期間；另有 {uncovered} 筆處置發生於上線前，不計）===")
    if not total:
        print("尚無可計分的處置事件——需要至少一天快照早於某次處置開始日。")
        return
    pct = f"（{len(hit)/total:.0%}）" if total >= 10 else "（樣本太小，先不談比率）"
    print(f"被處置 {total} 檔，事前示警 {len(hit)} 檔 {pct}")
    for v, a in hit:
        print(f"  ✅ {v['code']} {v.get('name')}：我方 {a['alerted_published_at'][:16]} 列為 "
              f"{a['alerted_level']} → 官方 {v['start']} 起處置")
    for v, _ in miss:
        print(f"  ❌ {v['code']} {v.get('name')}：官方 {v['start']} 起處置，我方事前未列入")

def audit():
    """狼來了守門員：把 forecast 的 danger/near 逐檔拿去比「我方 vs 官方近30日被點名次數」。
    單日 Precision 高不代表累積次數對——錯誤若叢聚在同一檔，就會累積成假 danger。
    上櫃無完整官方名單（端點不可回溯）→ 標「無官方資料」，勿誤判為亂喊。"""
    fp = os.path.join(HERE, "disposition-forecast.json")
    if not os.path.exists(fp):
        print("[audit] 尚無 disposition-forecast.json，先跑 `forecast`")
        return
    fc = json.load(open(fp))
    idx = official_index()
    avail = official_markets()
    axis = [d for d in sorted(idx) if d <= fc["as_of"]][-30:]
    tw_days = [d for d in axis if "TWSE" in avail.get(d, set())]
    otc_days = [d for d in axis if "TPEx" in avail.get(d, set())]
    print(f"[audit] as_of={fc['as_of']}；官方名單覆蓋 上市 {len(tw_days)} 日 / 上櫃 {len(otc_days)} 日")
    print(f"{'代號':>6} {'名稱':>10} {'狀態':>7} {'我方30日':>9} {'官方30日':>9}  判定")
    for s in fc["stocks"]:
        if s["status"] not in ("danger", "near"):
            continue
        mkt = "TWSE" if any(s["code"] in idx[d] and idx[d][s["code"]]["market"] == "TWSE"
                            for d in axis) else None
        days = tw_days if mkt == "TWSE" else otc_days
        n_off = sum(1 for d in days if s["code"] in idx[d])
        if len(days) < 10:
            verdict = "無官方資料（上櫃）"
        elif n_off == 0:
            verdict = "⚠️ 官方從未點名 ← 疑似亂喊"
        elif s["count_30d"] > n_off * 2:
            verdict = f"⚠️ 高估 {s['count_30d'] / max(n_off, 1):.1f}x"
        else:
            verdict = "✅ 對得上"
        print(f"{s['code']:>6} {s['name']:>10} {s['status']:>7} "
              f"{s['count_30d']:>9} {n_off:>9}  {verdict}")


# ── Phase 1：處置狀態機（rules[] → countdown/status/recent_hits）─────────
# 從注意到處置的四條累積路徑（見文件 §2）；「還差幾次」= 距最近那條門檻的天數。
DISP_PATHS = [
    ("連續3日款1", 3, "k1", "consec"),      # 連續 3 個營業日達款1
    ("連續5日",   5, "any", "consec"),      # 連續 5 個營業日達款1~7
    ("10日內6日", 6, "any", "win10"),       # 10 營業日內 6 日
    ("30日內12日", 12, "any", "win30"),     # 30 營業日內 12 日
]
REASON = {1: "近期漲跌劇烈", 2: "中長期漲幅過大", 3: "成交爆量",
          4: "週轉率過高", 6: "本益比/淨值比偏高", 7: "融資融券過熱"}
# 計入「還差幾次」的款別 —— 對齊官方（官方即以款1~7 累積）。
# 款2 的納入歷經三道修正（2026-08-03，缺任一道都會產生假 danger，勿單獨回退）：
#   1) 官方豁免「近30日曾公告款1 且 6日<25%」→ 過濾已冷卻的舊帳
#   2) `_price_break` 濾非交易因素跳空 → 修掉減資造成的假漲幅（虹光 2380）
#   3) `self_k1_history` proxy → 上櫃官方名單不可回溯，否則豁免形同失效（長尾 16 次 → 5 次）
# 驗證：`audit` 逐檔比對「我方 vs 官方近30日次數」（countdown 真正依賴的量，非單日 Precision）。
# 三道到齊後，納入款2 對 danger/near 完全無影響（2/0 不變）、僅 watch +3 → 安全且覆蓋更完整。
# 款6 仍排除：實測 0 命中（PB 來源落後一日 + 38% 案例需買不到的分點資料），計入等於加 0。
ACCUM_CLAUSES = {1, 2, 3, 4, 7}
# 官方名單來源的日子用這組：官方即以款1~7 累積，含我們算不出的款5（分點）與只算到 41% 的款6。
ACCUM_OFFICIAL = {1, 2, 3, 4, 5, 6, 7}

def _pred_dates():
    return sorted(f[5:13] for f in os.listdir(PRED) if f.startswith("pred_") and f.endswith(".json"))

_TIMES_RE = re.compile(r"第\s*(\d+)\s*次")

def _pub_trigger(t):
    """對外只給前端要用的欄位（base5/threshold 是內部診斷用，不必進資料契約）。"""
    return {k: t[k] for k in ("kind", "price", "pct", "margin") if k in t} if t else None

def _next_disposition(disp, countdown, cnt10):
    """處置中的股票：距「下一次處置」還差幾次、會是第幾次。
    2026-08-10 新制：撮合間隔與處置期間不再隨次數改變（一律約 2 分鐘 / 5 個營業日），
    30 日內第二次起加重的是**全面預收款券**。
    近 10 日無活動就不輸出（休眠股不該被算術距離誤判成快被加重）。"""
    if cnt10 == 0 or countdown >= 99:
        return {}
    m = _TIMES_RE.search(disp.get("times") or "")
    nxt = (int(m.group(1)) + 1) if m else 2
    # 不推測下次的撮合間隔：實測官方 2026-08 已改為「約每二分鐘」，寫死 5分/20分 會給錯數字。
    # 間隔只在官方公告原文裡才有，未公告前不知道 → 前端只講「措施加重」。
    return {"next_countdown": countdown, "next_times": f"第{nxt}次"}

def _last_trigger(axis, acc_days, k1_days):
    """逐日推進四條路徑，回「最後一次達門檻」的日期——達門檻＝當時就被處置，累積**重新起算**。
    少了這道，早已處置完畢的股票會被舊帳一路累加成假 danger
    （實例：統懋 2434 官方 30 日內 13 次已跨 12 次門檻＝7 月中就處置過，現只剩 1 次卻被算成剩0）。"""
    active, act_k1, last = [], [], None
    pos = {d: i for i, d in enumerate(axis)}
    for d in axis:
        if d in acc_days:
            active.append(d)
            if d in k1_days:
                act_k1.append(d)
        i = pos[d]
        w10 = set(axis[max(0, i - 9):i + 1])
        w30 = set(axis[max(0, i - 29):i + 1])
        if (_streak(axis[:i + 1], set(act_k1)) >= 3 or _streak(axis[:i + 1], set(active)) >= 5
                or len([x for x in active if x in w10]) >= 6
                or len([x for x in active if x in w30]) >= 12):
            last, active, act_k1 = d, [], []
    return last

def _streak(axis, hitset):
    """axis 由舊到新；回結尾連續在 hitset 的天數。"""
    s = 0
    for d in reversed(axis):
        if d in hitset:
            s += 1
        else:
            break
    return s

def forecast(as_of=None, window=30):
    """讀近 window 個交易日 prediction → 每檔累積狀態 → disposition-forecast.json。"""
    dates = [d for d in _pred_dates() if (as_of is None or d <= as_of)][-window:]
    if not dates:
        print("[forecast] 無 prediction，先跑 predict")
        return
    as_of = dates[-1]
    axis = dates                                        # 交易日軸（升冪）
    hitmap, meta, triggers = {}, {}, {}                  # code -> {date:{clauses}} / info / 明日觸發價
    for d in axis:
        p = json.load(open(os.path.join(PRED, f"pred_{d}.json")))
        if d == as_of:
            triggers = p.get("triggers", {}) or {}       # 只有最新一天的觸發價有意義
        for h in p.get("hits", []):
            c = h["code"]
            hitmap.setdefault(c, {})[d] = {r["clause"] for r in h.get("rules", [])}
            meta[c] = {"name": h.get("name"), "market": h.get("market"), "close": h.get("close")}
            if d == as_of:
                meta[c]["rules_today"] = h.get("rules", [])
    # ── 歷史「被點名次數」以**官方名單**為準（我們的預測只在官方覆蓋不到時遞補）──
    # countdown 數的是「官方公告注意的次數」，官方名單就是這個量的唯一真相；自家預測會漏
    # （實例：聯鈞 3450 官方近30日 11 次、我們只算到 1 次 → 誤判 safe）。
    # 上市每日皆有官方名單；上櫃端點不可回溯 → 沒官方的日子仍用自家預測。
    idx, avail = official_index(), official_markets()
    src_official = set()                                # (code, date) 來源為官方 → 用官方累積款別
    for d in axis:
        mkts = avail.get(d, set())
        if not mkts:
            continue
        for c, dm in hitmap.items():                    # 該市場當日改由官方認定，先清掉自家預測
            if d in dm and meta.get(c, {}).get("market") in mkts:
                del dm[d]
        for c, v in idx.get(d, {}).items():
            if v.get("market") not in mkts:
                continue
            hitmap.setdefault(c, {})[d] = set(v["clauses"])
            src_official.add((c, d))
            m = meta.setdefault(c, {})
            m.setdefault("name", v.get("name"))
            m.setdefault("market", v.get("market"))
            if m.get("close") is None:
                m["close"] = v.get("close")
    hitmap = {c: dm for c, dm in hitmap.items() if dm}

    # meta["close"] 原本取自「最後一次命中那天」的收盤，可能是好幾天前的舊價；
    # triggers 一定是 as_of 當天算的 → 用它校正，否則觸發價與顯示價對不起來。
    for c, t in triggers.items():
        if c in meta and t.get("close"):
            meta[c]["close"] = t["close"]

    disposed = disposed_set(as_of)
    announced = announced_set(as_of)                    # 已公布、尚未生效 → 已達標
    detail = disposed_detail(as_of)                     # 官方處置公告細節
    hist = alert_history()                              # 我們過去每天真的發布過的預警名單
    last10, last30 = axis[-10:], axis[-30:]
    stocks = []
    for c, dm in hitmap.items():
        # 計入 countdown 的「命中日」= 當天有 acute 款別（款1/3/4/7）
        # 官方來源的日子：官方本就以款1~7 累積（含我們算不出的款5/6）；自家預測的日子只採 ACCUM_CLAUSES
        acc = {}
        for d in dm:
            keep = dm[d] & (ACCUM_OFFICIAL if (c, d) in src_official else ACCUM_CLAUSES)
            if keep:
                acc[d] = keep
        any_days = set(acc)
        k1_days = {d for d in axis if 1 in dm.get(d, set())}
        # 期間若曾達門檻（＝已被處置一次）→ 累積重新起算，只算該日之後的命中
        trig = _last_trigger(axis, any_days, k1_days)
        if trig:
            acc = {d: v for d, v in acc.items() if d > trig}
            any_days = {d for d in any_days if d > trig}
            k1_days = {d for d in k1_days if d > trig}
        # 處置中的股票**繼續累積**：處置期間再達門檻 → 升級為下一次處置（撮合 5分→20分、全面預收）。
        # 起算點＝處置開始日，否則處置前的舊帳會被算進來。
        # disp＝「處置生效中」才有，供第二次處置的累積起算用；
        # disp_any 另含「已公布未生效」，戰績（_alerted）要用它，否則剛達標那批查不到示警紀錄。
        disp = detail.get(c) if c in disposed else None
        disp_any = detail.get(c) if (c in disposed or c in announced) else None
        if disp and disp.get("start"):
            st = disp["start"]
            acc = {d: v for d, v in acc.items() if d >= st}
            any_days = {d for d in any_days if d >= st}
            k1_days = {d for d in k1_days if d >= st}
        cons_any, cons_k1 = _streak(axis, any_days), _streak(axis, k1_days)
        cnt10 = sum(1 for d in last10 if d in acc)
        cnt30 = sum(1 for d in last30 if d in acc)
        # 款2/6 = context flag（不進 countdown）：近10日是否出現
        flags = {"k2_longterm": any(2 in dm.get(d, set()) for d in last10),
                 "k6_valuation": any(6 in dm.get(d, set()) for d in last10)}
        prog = {"consec_any": cons_any, "consec_k1": cons_k1, "win10": cnt10, "win30": cnt30}
        best, best_rem = None, 99
        for name, thr, kind, mode in DISP_PATHS:
            cur = cons_k1 if kind == "k1" else (cons_any if mode == "consec" else prog["win10"] if mode == "win10" else prog["win30"])
            # 連續型路徑：沒在連續中（cur==0）就不算「進行中」，避免單一跳空命中被算成「剩3」。
            if mode == "consec" and cur == 0:
                continue
            rem = max(0, thr - cur)
            if rem < best_rem:
                best_rem, best = rem, name
        countdown = best_rem if best is not None else 99
        if c in disposed:
            status = "disposed"
        elif c in announced or trig == as_of:
            # 已達處置門檻、等生效：官方已公布（announced）或我們今天算到達標（trig==as_of）。
            # 少了這一級，達標當天計數歸零後會被判成 safe → 使用者昨天看到「明天進處置」，
            # 今天卻整檔從畫面消失（2026-08-07 川湖等 5 檔即如此）。
            status = "pending"
        elif cnt10 == 0:
            status = "safe"                       # 近10日無 acute 活動 → 休眠，不論算術距離
        elif countdown <= 1:
            status = "danger"
        elif countdown == 2:
            status = "near"
        elif countdown <= 4:
            status = "watch"
        else:
            status = "safe"
        # 白話原因（取最近 acute 命中日的款別；無則用 flag）
        last_hit = max(any_days) if any_days else None
        cls = acc.get(as_of) or (acc.get(last_hit) if last_hit else set())
        reason = " + ".join(dict.fromkeys(REASON[x] for x in sorted(cls) if x in REASON))
        if not reason:
            reason = "中長期漲幅偏高（觀察）" if flags["k2_longterm"] else "—"
        recent = [{"date": d, "hit": d in acc, "k1": 1 in dm.get(d, set())} for d in last10]
        stocks.append({
            "code": c, "name": meta[c]["name"], "market": meta[c]["market"], "close": meta[c]["close"],
            "status": status, "countdown": countdown, "path": best,
            "consecutive": cons_any, "consecutive_k1": cons_k1,
            "count_10d": cnt10, "count_30d": cnt30, "flags": flags,
            "plain_reason": reason, "recent_hits": recent,
            "rules_today": meta[c].get("rules_today", []),
            "disposal": detail.get(c) if status in ("disposed", "pending") else None,
            **(_next_disposition(disp, countdown, cnt10) if disp else {}),
            **(_alerted(hist, c, disp_any["start"]) if disp_any and disp_any.get("start") else {}),
            "trigger": _pub_trigger(triggers.get(c)),
        })
    # 官方處置中、但近期沒被我們預測到的股票也要列出（處置中＝官方事實，非預測）
    seen = {s["code"] for s in stocks}
    for c in disposed | announced:
        if c in seen:
            continue
        d = detail.get(c, {})
        stocks.append({
            "code": c, "name": d.get("name"), "market": "TWSE" if d.get("market") == "市" else "TPEx",
            "close": None, "status": "pending" if c in announced else "disposed",
            "countdown": 0, "path": None,
            "consecutive": 0, "consecutive_k1": 0, "count_10d": 0, "count_30d": 0,
            "flags": {}, "recent_hits": [], "rules_today": [],
            "plain_reason": "已公布處置，尚未生效" if c in announced else "官方處置中",
            "disposal": d,
            **(_alerted(hist, c, d["start"]) if d.get("start") else {}),
        })
    rank = {"disposed": 0, "pending": 1, "danger": 2, "near": 3, "watch": 4, "safe": 5}
    stocks.sort(key=lambda s: (rank[s["status"]], s["countdown"], -s["count_30d"]))
    out = {"as_of": as_of, "generated_at": _now(), "window": len(axis),
           "counts": {k: sum(1 for s in stocks if s["status"] == k) for k in rank},
           "stocks": stocks}
    fp = os.path.join(HERE, "disposition-forecast.json")
    json.dump(out, open(fp, "w"), ensure_ascii=False, indent=2)
    save_forecast_snapshot(out)          # 戰績快照，事後無法重建 → 每天存，進 git
    n_new, n_all = save_disposals()      # 處置事件歷史（官方 API 只回進行中的）
    if n_new:
        print(f"[forecast] 新增 {n_new} 筆處置事件（累計 {n_all}）")
    print(f"[forecast] as_of={as_of} 軸長={len(axis)}日 → {fp}")
    print("  " + " ".join(f"{k}={out['counts'][k]}" for k in rank))
    top = [s for s in stocks if s["status"] in ("danger", "near")][:12]
    for s in top:
        print(f"  {s['status']:8s} 剩{s['countdown']}次 {s['code']} {s['name']}"
              f"（連{s['consecutive']}日/近10日{s['count_10d']}次/{s['path']}）：{s['plain_reason']}")

def _now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def _today(): return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y%m%d")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if cmd == "predict":
        predict(sys.argv[2] if len(sys.argv) > 2 else _today())
    elif cmd == "notice":
        notice()
    elif cmd == "backfill-notice":
        backfill_notice(sys.argv[2] if len(sys.argv) > 2 else None,
                        sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "reconcile":
        reconcile()
    elif cmd == "audit":
        audit()
    elif cmd == "record":
        record()
    elif cmd == "forecast":
        forecast(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "backtest":
        backtest(int(sys.argv[2]) if len(sys.argv) > 2 else 30)
    elif cmd == "explain":
        explain(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "daily":
        # 先抓官方名單再 predict：當日公告會餵給款2 豁免
        backfill_notice(); notice(); predict(_today())
    else:
        print(__doc__)
