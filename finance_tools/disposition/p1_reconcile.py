#!/usr/bin/env python3
"""處置股預測引擎 + Official Notice Reconciliation（P1 驗證框架）。

多款規則引擎：每檔命中股票輸出 `rules[]`，逐條列出「命中哪款/哪標準/實際值 vs 門檻/理由」。
款別實作狀態（見 docs/future/處置股預測.md §3）：
  款1 ✅  款2 ✅  款3 ✅  款4 ✅  款6 ⚠️(PE/PB;PB為openapi最新日)  款7 ⚠️(margin為最新日,forward準)
  款5 ❌ 分點資料不可得；款8 TDR 不做。
每日 forward 累積官方標籤（notice API 無法回補歷史）。

用法：
  python3 p1_reconcile.py predict [YYYYMMDD]   # 算某收盤日全款 prediction（省略=今天）
  python3 p1_reconcile.py notice               # 抓當前官方 notice 快照（存原文）
  python3 p1_reconcile.py reconcile            # 對帳款1 prediction vs 官方款1 → confusion matrix
  python3 p1_reconcile.py explain CODE [DATE]  # 印某檔的完整命中理由
  python3 p1_reconcile.py daily                # predict(today)+notice 一次做

注意：官方款1紀錄日=公告日，觸發在前一交易日（off-by-one，見 §4.4）。
"""
import json, os, sys, re, urllib.request, datetime, statistics

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE, PRED, NOTI = (os.path.join(HERE, d) for d in ("cache", "predictions", "notices"))
UA = {"User-Agent": "Mozilla/5.0"}

# ── 各款門檻（market-specific，見文件 §3）──────────────────────
CFG = {
    "TWSE": {"k1": {"std1": 32.0, "std2": 25.0, "gap": 0.0},
             "k2": {30: (100.0, 85.0), 60: (130.0, 110.0), 90: (160.0, 135.0)},
             "k2_low": None,                       # 上市無低價股特別門檻
             "k4_turn": 10.0},
    "TPEx": {"k1": {"std1": 30.0, "std2": 27.0, "gap": 0.0},
             "k2": {30: (100.0, 80.0), 60: (130.0, 80.0), 90: (160.0, 80.0)},
             "k2_low": {30: 120.0, 60: 180.0, 90: 240.0},   # 收盤<5元 上櫃特別門檻
             "k4_turn": 5.0},
}
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

def fj(url, tries=3):
    last = None
    for _ in range(tries):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read())
        except Exception as e:
            last = e
    raise last

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
    series = _series(loader, market_date, 90)
    if not series or series[-1][0] != market_date:
        return []
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
            "pe": row.get("pe") if row.get("pe") is not None else pe_api.get(c),
            "pb": pb.get(c), "shares": sh, "turnover": turn,
            "vol_ratio": (row["vol"] / va60) if (va60 and row.get("vol")) else None,
            "ret6": _ret(series, c, 6),
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
    # 加權 PE/PB（cap = close×shares）
    mkt_pe_w = _cap_wavg([(M[c]["pe"], (M[c]["close"] or 0) * (M[c]["shares"] or 0)) for c in M])
    mkt_pb_w = _cap_wavg([(M[c]["pb"], (M[c]["close"] or 0) * (M[c]["shares"] or 0)) for c in M])
    sec_pb_w = {}
    for c in M:
        sec_pb_w.setdefault(M[c]["sector"], []).append((M[c]["pb"], (M[c]["close"] or 0) * (M[c]["shares"] or 0)))
    sec_pb_w = {s: _cap_wavg(v) for s, v in sec_pb_w.items()}

    def peer_ok(s, peer_n, c):
        """款1類股比較是否適用：同類≥5 且 PE 非(<0或≥60)。"""
        p = M[c]["pe"]
        return peer_n.get(s, 0) >= MIN_PEERS and not (p is not None and (p < PE_LO or p >= PE_HI))

    hits = {}
    def add(c, rule):
        hits.setdefault(c, {"code": c, "name": M[c]["name"], "market": market,
                            "close": M[c]["close"], "rules": []})["rules"].append(rule)

    for c in M:
        m = M[c]
        cl = m["close"]
        if cl is None or cl < PRICE_FLOOR:
            continue
        s = m["sector"]

        # ── 款1 ──（並算出 k1_25 供款3/4/7 前置）
        k1_25 = False
        if m["ret6"] is not None:
            a, mkt6, peer6, pn6 = m["ret6"], A6[0], A6[1], A6[2]
            papp = peer_ok(s, pn6, c)
            mdiff = abs(a - mkt6) if mkt6 is not None else None
            pdiff = abs(a - peer6[s]) if (papp and s in peer6) else None
            m_ok = mdiff is not None and mdiff >= DIFF
            p_ok = (pdiff is not None and pdiff >= DIFF) if papp else True
            t = cfg["k1"]
            base_ok = abs(a) > t["std1"] or (abs(a) > t["std2"] and abs(cl - series[-6][1].get(c, {}).get("close", cl)) >= t["gap"])
            if base_ok and m_ok and p_ok:
                std = "32%版" if abs(a) > t["std1"] else "25%版"
                add(c, {"clause": 1, "standard": std,
                        "metrics": {"return_6d": round(a, 2), "market_diff": round(mdiff, 2) if mdiff else None,
                                    "peer_diff": round(pdiff, 2) if pdiff is not None else None,
                                    "peer_applicable": papp},
                        "thresholds": {"return": t["std1"] if std == "32%版" else t["std2"], "diff": DIFF},
                        "reason": f"6日累積{a:+.1f}%(>{t['std2' if std=='25%版' else 'std1']}%) 且 與大盤差{mdiff:.1f}%"
                                  + (f"、與同類差{pdiff:.1f}%(均≥20%)" if papp and pdiff is not None else "、同類比較豁免")})
            # k1_25：符合款1「25%版」門檻（供前置）
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
            # 豁免：近60日曾處置 且 6日累積<10%
            if c in disposed and m["ret6"] is not None and abs(m["ret6"]) < K2_EX_DISPOSED_RET:
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

    return list(hits.values())


def predict(market_date):
    days = trading_days_back(market_date, 6)
    if len(days) < 6:
        print(f"[predict] {market_date} 交易日不足6天（需先回補快取）: {days}")
        return
    sector, pe_api = sector_pe()
    shares_tw, pb, margin = ref_data()
    official_hist = official_k1_by_date()
    disposed = disposed_set()
    hits = []
    for mkt in ("TWSE", "TPEx"):
        try:
            hits += rules_for_market(mkt, market_date, sector, pe_api, shares_tw, pb, margin, official_hist, disposed)
        except Exception as e:
            print(f"[predict] {mkt} 引擎錯誤: {e}")
    rec = {"market_date": market_date, "prediction_generated_at": _now(),
           "engine": "multi-clause-v2", "clauses": [1, 2, 3, 4, 6, 7],
           "predicted_codes": [h["code"] for h in hits], "hits": hits}
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
    hist, disp = official_k1_by_date(), disposed_set()
    for d in days:
        hits = []
        for mkt in ("TWSE", "TPEx"):
            try:
                hits += rules_for_market(mkt, d, sector, pe, shares, pb, margin, hist, disp)
            except Exception as e:
                print(f"  {d} {mkt} err: {e}")
        rec = {"market_date": d, "prediction_generated_at": _now(),
               "engine": "multi-clause-v2", "clauses": [1, 2, 3, 4, 6, 7],
               "predicted_codes": [h["code"] for h in hits], "hits": hits}
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

def refresh_punish():
    tw_cp = os.path.join(CACHE, "punish_cache.json")
    otc_cp = os.path.join(CACHE, "otc_punish_cache.json")
    try:
        json.dump(fj("https://openapi.twse.com.tw/v1/announcement/punish"), open(tw_cp, "w"))
    except Exception as e:
        print(f"[punish] 上市抓取失敗，用快取: {e}")
    try:
        json.dump(fj("https://www.tpex.org.tw/openapi/v1/tpex_disposal_information"), open(otc_cp, "w"))
    except Exception as e:
        print(f"[punish] 上櫃抓取失敗，用快取: {e}")

def disposed_set():
    out = set()
    tw_cp = os.path.join(CACHE, "punish_cache.json")
    if os.path.exists(tw_cp):
        out |= {p["Code"] for p in json.load(open(tw_cp)) if _is_stock(p.get("Code", ""))}
    otc_cp = os.path.join(CACHE, "otc_punish_cache.json")
    if os.path.exists(otc_cp):
        out |= {p["SecuritiesCompanyCode"] for p in json.load(open(otc_cp)) if _is_stock(p.get("SecuritiesCompanyCode", ""))}
    return out

def _k1_predicted(pred):
    """從 prediction 取出「命中款1」的代號集合（多款引擎後 reconcile 只比款1）。"""
    out = set()
    for h in pred.get("hits", []):
        if any(r["clause"] == 1 for r in h.get("rules", [])) and _is_stock(h["code"]):
            out.add(h["code"])
    return out

def reconcile():
    preds = {}
    for f in os.listdir(PRED):
        if f.startswith("pred_"):
            p = json.load(open(os.path.join(PRED, f)))
            preds[p["market_date"]] = p
    official = official_k1_by_date()
    disposed = disposed_set()
    TP = FP = FN = 0
    fn_rows, unavail = [], []
    for mdate in sorted(preds):
        pset = _k1_predicted(preds[mdate])       # 只取款1預測
        if mdate not in official:
            unavail.append(mdate)
            continue
        oset = set(official[mdate])
        valid = oset | disposed
        TP += len(pset & valid); FP += len(pset - valid); FN += len(oset - pset)
        for c in (oset - pset):
            fn_rows.append((mdate, c, official[mdate][c]))
    print(f"\n=== Reconcile 款1（GT=notetrans+處置名單，排除權證）===")
    print(f"TP={TP} FP={FP} FN={FN}")
    if TP + FP: print(f"Precision={TP/(TP+FP):.1%}")
    if TP + FN: print(f"Recall={TP/(TP+FN):.1%}")
    if unavail:
        print(f"GT unavailable（官方尚未涵蓋，不計入）: {unavail}")
    if fn_rows:
        print("FN（官方款1、我未預測 ← 金礦）:")
        for d, c, n in fn_rows:
            print(f"  {d} {c} {n}")
    for mdate in sorted(preds):
        if mdate in official:
            fp = _k1_predicted(preds[mdate]) - set(official[mdate]) - disposed
            if fp: print(f"FP {mdate}: {sorted(fp)}")

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
# 計入「還差幾次」的款別：只算 acute（款1-anchored、反映當下波動）。
# 款2(中長期)/款6(結構)易因「舊帳」持續累積 → 降為 context flag，不進 countdown。
# （官方雖計款1~7，但其款2 豁免機制正是為過濾冷卻股；我們資料未齊前先保守。）
ACCUM_CLAUSES = {1, 3, 4, 7}

def _pred_dates():
    return sorted(f[5:13] for f in os.listdir(PRED) if f.startswith("pred_") and f.endswith(".json"))

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
    hitmap, meta = {}, {}                               # code -> {date:{clauses}} / code -> info
    for d in axis:
        p = json.load(open(os.path.join(PRED, f"pred_{d}.json")))
        for h in p.get("hits", []):
            c = h["code"]
            hitmap.setdefault(c, {})[d] = {r["clause"] for r in h.get("rules", [])}
            meta[c] = {"name": h.get("name"), "market": h.get("market"), "close": h.get("close")}
            if d == as_of:
                meta[c]["rules_today"] = h.get("rules", [])
    disposed = disposed_set()
    last10, last30 = axis[-10:], axis[-30:]
    stocks = []
    for c, dm in hitmap.items():
        # 計入 countdown 的「命中日」= 當天有 acute 款別（款1/3/4/7）
        acc = {d: (dm[d] & ACCUM_CLAUSES) for d in dm if dm[d] & ACCUM_CLAUSES}
        any_days = set(acc)
        k1_days = {d for d in axis if 1 in dm.get(d, set())}
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
        })
    rank = {"disposed": 0, "danger": 1, "near": 2, "watch": 3, "safe": 4}
    stocks.sort(key=lambda s: (rank[s["status"]], s["countdown"], -s["count_30d"]))
    out = {"as_of": as_of, "generated_at": _now(), "window": len(axis),
           "counts": {k: sum(1 for s in stocks if s["status"] == k) for k in rank},
           "stocks": stocks}
    fp = os.path.join(HERE, "disposition-forecast.json")
    json.dump(out, open(fp, "w"), ensure_ascii=False, indent=2)
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
    elif cmd == "reconcile":
        reconcile()
    elif cmd == "forecast":
        forecast(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "backtest":
        backtest(int(sys.argv[2]) if len(sys.argv) > 2 else 30)
    elif cmd == "explain":
        explain(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "daily":
        predict(_today()); notice()
    else:
        print(__doc__)
