#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data/us.db → us_api/（前端读取的静态 JSON）

  us_api/overview.json        指数卡片、估值卡片（PE / CAPE / 股权溢价 / VIX）、半导体相对强弱
  us_api/series/<id>.json     走势序列 {"dates":[...], "values":[...]}：
                              spx ndx sox vix erp pe cape sox_ndx
  us_api/qvix/<idx>.json      A 股指数页用的 QVIX：csi300 / cyb / star50

标普500 日度 PE 的算法（multpl 只有月度 PE）：
  每月的 TTM EPS ≈ 当月标普500 收盘均价 ÷ 当月 PE；当月最后一行若是"当前估计值"（日期不是 1 号），
  用那一天的收盘价 ÷ 该值，更贴近当下。日度 PE = 每日收盘 ÷ 所在月（没有就用最近一个月）的 EPS。
股权溢价 = 100 ÷ 日度PE − 美国10年国债收益率（取 ≤ 当日最近一个值），与沪深300 同一公式。
"""
import os, sys, json, sqlite3, bisect
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "data", "us.db")
OUT = os.path.join(ROOT, "us_api")
INDEX_NAMES = {"spx": ("标普500", "SPX"), "ndx": ("纳斯达克100", "NDX"), "sox": ("费城半导体", "SOX")}
QVIX = {"csi300": ("qvix_csi300", "沪深300ETF期权 QVIX"), "cyb": ("qvix_cyb", "创业板期权 QVIX"), "star50": ("qvix_star50", "科创板期权 QVIX")}


def load(c, mid):
    return [(d, v) for d, v in c.execute("SELECT date, value FROM obs WHERE metric=? AND value IS NOT NULL ORDER BY date", (mid,))]


def cut_date(years, today=None):
    t = today or date.today()
    return (t - timedelta(days=round(365.25 * years))).isoformat() if years else "0000-01-01"


def window(pts, years, today=None):
    c = cut_date(years, today)
    return [v for d, v in pts if d >= c]


def pos(vals, x):
    """当前值在历史中的百分位（≤x 的占比），0~1"""
    if not vals or x is None:
        return None
    return round(sum(1 for v in vals if v <= x) / len(vals), 4)


def quant(vals, p):
    s = sorted(vals)
    if not s:
        return None
    k = (len(s) - 1) * p
    f = int(k)
    return s[f] if f + 1 >= len(s) else s[f] + (s[f + 1] - s[f]) * (k - f)


def qset(vals, dp=4):
    if len(vals) < 20:
        return None
    return {"p20": round(quant(vals, .2), dp), "p50": round(quant(vals, .5), dp), "p80": round(quant(vals, .8), dp)}


def series_json(pts, dp):
    return {"dates": [d for d, _ in pts], "values": [round(v, dp) for _, v in pts]}


def daily_pe(spx, pe_m):
    """→ [(日期, 日度PE)]；见文件头说明"""
    if not spx or not pe_m:
        return []
    by_month = {}
    for d, v in spx:
        by_month.setdefault(d[:7], []).append(v)
    sdates = [d for d, _ in spx]
    eps_month = {}
    last_row = max(pe_m)[0]
    for d, pe in sorted(pe_m):            # 同月多行时后面的覆盖前面的（当前估计值在 1 号那行之后）
        if not pe or pe <= 0:
            continue
        m = d[:7]
        if d == last_row and not d.endswith("-01"):
            i = bisect.bisect_right(sdates, d) - 1
            if i < 0:
                continue
            eps_month[m] = spx[i][1] / pe
        elif m in by_month:
            eps_month[m] = (sum(by_month[m]) / len(by_month[m])) / pe
    months = sorted(eps_month)
    out = []
    for d, v in spx:
        j = bisect.bisect_right(months, d[:7]) - 1
        if j >= 0:
            out.append((d, v / eps_month[months[j]]))
    return out


def asof_join(dates, pts):
    """对每个日期取 pts 中 ≤ 该日期的最近值"""
    pd_ = [d for d, _ in pts]
    out = []
    for d in dates:
        i = bisect.bisect_right(pd_, d) - 1
        out.append(pts[i][1] if i >= 0 else None)
    return out


def index_card(pts):
    if len(pts) < 2:
        return None
    d, last = pts[-1]
    prev = pts[-2][1]
    y0 = [v for dd, v in pts if dd < d[:4] + "-01-01"]
    ath = max(v for _, v in pts)
    return {"date": d, "close": round(last, 2), "chg": round(last - prev, 2), "chg_pct": round(last / prev - 1, 6),
            "ytd_pct": round(last / y0[-1] - 1, 6) if y0 else None, "ath": round(ath, 2), "dd": round(last / ath - 1, 6)}


def export(db=DB, out=OUT, today=None):
    c = sqlite3.connect(db)
    S = {m: load(c, m) for m in ("ndx", "sox", "spx", "vix", "vix_high", "vix_low", "pe_m", "cape_m", "us10y", *[v[0] for v in QVIX.values()])}
    c.close()
    os.makedirs(os.path.join(out, "series"), exist_ok=True)
    os.makedirs(os.path.join(out, "qvix"), exist_ok=True)

    def dump(path, obj):
        with open(os.path.join(out, path), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))

    ov = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
          "cards": {}, "val": {}, "ratio": None, "names": {k: v[0] for k, v in INDEX_NAMES.items()}}
    for k, (nm, code) in INDEX_NAMES.items():
        card = index_card(S[k])
        if card:
            ov["cards"][k] = {"name": nm, "code": code, **card}
        if S[k]:
            dump(f"series/{k}.json", series_json(S[k], 2))

    # 标普500 PE / 股权溢价
    ped = daily_pe(S["spx"], S["pe_m"])
    if ped:
        dump("series/pe.json", series_json(ped, 2))
        pm = [v for _, v in S["pe_m"]]
        ov["val"]["pe"] = {"date": ped[-1][0], "value": round(ped[-1][1], 2), "monthly_date": S["pe_m"][-1][0],
                           "pct_5y": pos(window(ped, 5, today), ped[-1][1]), "pct_10y": pos(window(ped, 10, today), ped[-1][1]),
                           "median_all": round(quant(pm, .5), 2), "since": S["pe_m"][0][0][:4]}
        if S["us10y"]:
            y = asof_join([d for d, _ in ped], S["us10y"])
            erp = [(d, 100.0 / pe - yy) for (d, pe), yy in zip(ped, y) if yy is not None and pe > 0]
            if erp:
                dump("series/erp.json", series_json(erp, 3))
                cur = erp[-1][1]
                w5, w10 = window(erp, 5, today), window(erp, 10, today)
                ov["val"]["erp"] = {"date": erp[-1][0], "value": round(cur, 3), "pct_5y": pos(w5, cur), "pct_10y": pos(w10, cur),
                                    "q5y": qset(w5, 3), "q10y": qset(w10, 3), "us10y": round(y[-1], 3), "ey": round(100 / ped[-1][1], 3),
                                    "us10y_date": S["us10y"][-1][0]}
    # 席勒 CAPE（月度）
    if S["cape_m"]:
        dump("series/cape.json", series_json(S["cape_m"], 2))
        cv = [v for _, v in S["cape_m"]]
        cur = cv[-1]
        ov["val"]["cape"] = {"date": S["cape_m"][-1][0], "value": round(cur, 2),
                             "pct_5y": pos(window(S["cape_m"], 5, today), cur), "pct_10y": pos(window(S["cape_m"], 10, today), cur), "mean_all": round(sum(cv) / len(cv), 2),
                             "q_all": qset(cv, 2), "since": S["cape_m"][0][0][:4]}
    # VIX
    if S["vix"]:
        hi_m, lo_m = dict(S["vix_high"]), dict(S["vix_low"])
        vj = series_json(S["vix"], 2)
        if hi_m and lo_m:                      # 日内最高 / 最低，与收盘按日期对齐（缺的为 null）
            vj["high"] = [round(hi_m[d], 2) if d in hi_m else None for d in vj["dates"]]
            vj["low"] = [round(lo_m[d], 2) if d in lo_m else None for d in vj["dates"]]
        dump("series/vix.json", vj)
        vv = [v for _, v in S["vix"]]
        cur = vv[-1]
        prev = vv[-2] if len(vv) > 1 else None
        d_last = S["vix"][-1][0]
        ov["val"]["vix"] = {"date": d_last, "value": round(cur, 2), "chg": round(cur - prev, 2) if prev else None,
                            "high": round(hi_m[d_last], 2) if d_last in hi_m else None,
                            "low": round(lo_m[d_last], 2) if d_last in lo_m else None,
                            "pct_5y": pos(window(S["vix"], 5, today), cur), "pct_10y": pos(window(S["vix"], 10, today), cur),
                            "q10y": qset(window(S["vix"], 10, today), 2), "since": S["vix"][0][0][:4]}
    # 半导体相对强弱 SOX ÷ NDX（按 NDX 日期对齐）
    if S["sox"] and S["ndx"]:
        sm = dict(S["sox"])
        r = [(d, sm[d] / v) for d, v in S["ndx"] if d in sm and v]
        if r:
            dump("series/sox_ndx.json", series_json(r, 5))
            w5 = window(r, 5, today)
            ov["ratio"] = {"date": r[-1][0], "value": round(r[-1][1], 5), "pct_5y": pos(w5, r[-1][1]), "q5y": qset(w5, 5),
                           "pct_10y": pos(window(r, 10, today), r[-1][1]), "since": r[0][0][:4]}
    # QVIX（A 股指数页用）
    for idx, (mid, nm) in QVIX.items():
        pts = S[mid]
        if not pts:
            continue
        vals = [v for _, v in pts]
        cur = vals[-1]
        w5 = window(pts, 5, today)
        dump(f"qvix/{idx}.json", {"name": nm, **series_json(pts, 2), "latest": {"date": pts[-1][0], "value": round(cur, 2)},
                                  "pct_5y": pos(w5, cur), "pct_10y": pos(window(pts, 10, today), cur), "q5y": qset(w5, 2),
                                  "median_all": round(quant(vals, .5), 2), "since": pts[0][0][:4]})
    dump("overview.json", ov)
    print(f"导出完成 → {out}：指数卡片 {list(ov['cards'])}，估值 {list(ov['val'])}，相对强弱 {'有' if ov['ratio'] else '无'}，"
          f"QVIX {[k for k in QVIX if os.path.exists(os.path.join(out, 'qvix', k + '.json'))]}")
    return ov


def selftest():
    import tempfile, shutil
    td = tempfile.mkdtemp()
    try:
        db = os.path.join(td, "data", "us.db")
        os.makedirs(os.path.dirname(db))
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE obs(metric TEXT, date TEXT, value REAL, source TEXT, PRIMARY KEY(metric,date))")
        rows = []
        d0 = date(2015, 1, 1)
        for i in range(0, 4000):
            d = (d0 + timedelta(days=i)).isoformat()
            rows += [("spx", d, 2000 + i), ("ndx", d, 4000 + i), ("sox", d, 600 + i * 0.5), 
                     ("vix", d, 10 + (i % 30)), ("vix_high", d, 12 + (i % 30)), ("vix_low", d, 9 + (i % 30)),
                     ("us10y", d, 2.0), ("qvix_csi300", d, 15 + (i % 10))]
        for y in range(2015, 2026):
            for m in range(1, 13):
                rows += [("pe_m", f"{y}-{m:02d}-01", 20.0), ("cape_m", f"{y}-{m:02d}-01", 25 + (y - 2015))]
        last = (d0 + timedelta(days=3999)).isoformat()
        rows.append(("pe_m", last, 25.0))                  # 当前估计值行（不是 1 号）
        c.executemany("INSERT INTO obs VALUES (?,?,?,NULL)", rows)
        c.commit(); c.close()
        ov = export(db, os.path.join(td, "us_api"), today=date.fromisoformat(last))
        # 日度 PE：最后一天应等于当前估计值 25（同一天价格 ÷ 该天 EPS）
        assert abs(ov["val"]["pe"]["value"] - 25.0) < 1e-6, ov["val"]["pe"]
        # ERP = 100/25 − 2 = 2
        assert abs(ov["val"]["erp"]["value"] - 2.0) < 1e-6 and ov["val"]["erp"]["q5y"], ov["val"]["erp"]
        # 普通月份：EPS = 当月均价 ÷ 20 → 当月均价那天 PE≈20
        s = json.load(open(os.path.join(td, "us_api", "series", "pe.json")))
        i = s["dates"].index("2016-06-15"); assert 19.5 < s["values"][i] < 20.5, s["values"][i]
        cd = ov["cards"]["spx"]; assert cd["chg"] == 1 and cd["dd"] == 0 and cd["ytd_pct"] > 0, cd
        assert list(ov["cards"]) == ["spx", "ndx", "sox"]
        assert ov["ratio"]["value"] > 0 and ov["val"]["vix"]["q10y"]["p50"] > 0 and ov["val"]["pe"]["pct_10y"] is not None
        q = json.load(open(os.path.join(td, "us_api", "qvix", "csi300.json")))
        assert q["latest"]["date"] == last and q["q5y"] and len(q["dates"]) == 4000
        assert not os.path.exists(os.path.join(td, "us_api", "qvix", "cyb.json"))   # 没数据不生成
        v = json.load(open(os.path.join(td, "us_api", "series", "vix.json")))
        assert len(v["high"]) == len(v["dates"]) and v["high"][-1] - v["values"][-1] == 2 and v["low"][-1] == v["values"][-1] - 1
        assert ov["val"]["vix"]["high"] == ov["val"]["vix"]["value"] + 2
        assert quant([1, 2, 3, 4], .5) == 2.5 and pos([1, 2, 3, 4], 2) == 0.5
        print("selftest OK")
    finally:
        shutil.rmtree(td)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    export()
