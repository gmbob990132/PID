#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T助手 · 每日计算（北京时间每个交易日 21:00 由 t-daily 工作流运行）

对 tech_config/stocks.csv 里 focus=1 的股票：
  1) 取前复权日线（A 股 Tushare，港股腾讯；每次都取全量约 11 年，新加入的股票自然也是全量）
  2) 按 t_rules 第四版计算：趋势、指标、条件、第一/二档支撑压力
  3) 分钟线均线：A 股新浪 60/30 分钟（不复权 → 用日线换算成前复权）；
     港股东方财富 60/30 分钟（每次只给约一个月，存进 data/t_min.sqlite 逐日积累）
  4) 历史统计：过去 10 年里"同一趋势、同一侧条件满足数 ≥ 今天"的交易日，次日触及支撑/压力的比例，
     以及触及后的当日、1/2/3 日表现（本股 + 全部重点股合并）
  5) 输出 t_api/overview.json（列表与明细）、t_api/kline/<代码>.json（近 160 日 K 线与均线）
置顶列表：tech_config/t_pins.csv（页面上点星标会直接修改它；页面自己读这个文件，不需要重新计算）
只提供客观的指标状态与历史统计，不构成任何建议。
"""
import json
import os
import sqlite3
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

import t_rules as R
import t_backtest as BT
import t_exit as TX

CN_TZ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
STOCKS_CSV = os.path.join(HERE, "tech_config", "stocks.csv")
NAMES_JSON = os.path.join(HERE, "tech_api", "stocks.json")
MIN_DB = os.path.join(HERE, "data", "t_min.sqlite")
OUT = os.path.join(HERE, "t_api")
VER = 4
YEARS = 10
UA = BT.UA

IND_NOTE = {
    "rsi6": "6 日相对强弱指数：近 6 天上涨力度占总波动的比例。低于 20 常称超卖区，高于 80 常称超买区。",
    "rsi24": "24 日相对强弱指数：同上，周期更长、更平滑。低于 30 偏弱，高于 70 偏强。",
    "bias20": "收盘价偏离 20 日均线的百分比。括号里是本股过去 10 年的分位：0% = 历史上偏离最低，100% = 最高。",
    "bias55": "收盘价偏离 55 日均线的百分比，含义同上。",
    "kdj": "KDJ(9,3,3)：反映收盘价在近 9 日高低区间里的位置。J 值最敏感，低于 0 或高于 100 表示处在区间的极端位置。",
    "macd": "MACD(12,26,9)：DIF 为快慢均线差，DEA 为其均线，柱子 = 2×(DIF−DEA)。柱子变长/缩短反映动能的增强/减弱。",
    "boll": "布林线(20,2)：中轨为 20 日均线，上下轨为 ±2 倍标准差。%B 表示价格在通道中的位置：0% = 下轨，100% = 上轨。",
    "vr": "量比：当日成交量 ÷ 近 20 日平均成交量。大于 1 表示放量，小于 1 表示缩量。",
    "atr": "ATR：近 14 日平均真实波幅，衡量一天的典型波动幅度；预计波动区间 = 收盘价 ± 0.7×ATR。",
    "trend": "趋势：MA20>MA55、收盘>MA55 且 MA20 高于 5 天前为上涨；三条都反过来为下跌；其余为震荡。"
             "长期：收盘价在 MA233 之上为长期多头，之下为长期空头。",
}


def f(x, dp=4):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(x) else round(x, dp)


# ---------------- 股票与名称 ----------------
def load_focus():
    df = pd.read_csv(STOCKS_CSV, dtype=str, encoding="utf-8-sig").fillna("")
    df = df[df["focus"].str.strip() == "1"]
    names = {}
    try:
        with open(NAMES_JSON, encoding="utf-8") as fh:
            names = {s["code"]: s.get("name") or "" for s in json.load(fh).get("stocks", [])}
    except Exception:  # noqa: BLE001
        pass
    out = []
    for _, r in df.iterrows():
        c = r["code"].strip()
        if c:
            out.append((c, r["name"].strip() or names.get(c, "")))
    return out


# ---------------- 分钟线 ----------------
EM_FAILS = 0          # 东财港股分钟线连续失败次数（GitHub 服务器上经常被直接断开）
def fetch_hk_min_em(code, klt):
    num = code.split(".")[0].zfill(5)
    global EM_FAILS
    if EM_FAILS >= 3:                    # 本次运行里东财已经连续失败 3 次：后面直接用腾讯备用，不再白等
        raise RuntimeError("东财本次运行已连续失败，跳过")
    last = None
    time.sleep(1.0)
    for attempt in range(2):
        host = ["push2his.eastmoney.com", "33.push2his.eastmoney.com"][attempt]
        url = (f"https://{host}/api/qt/stock/kline/get?secid=116.{num}&fields1=f1"
               f"&fields2=f51,f52,f53,f54,f55&klt={klt}&fqt=0&end=20500101&lmt=300")
        try:
            with urlopen(Request(url, headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}),
                         timeout=20) as r:
                js = json.loads(r.read().decode("utf-8", "replace"))
            kl = ((js.get("data") or {}).get("klines")) or []
            if not kl:
                raise RuntimeError("返回空")
            rows = []
            for s in kl:
                a = s.split(",")
                rows.append({"dt": a[0], "open": float(a[1]), "close": float(a[2]),
                             "high": float(a[3]), "low": float(a[4])})
            EM_FAILS = 0
            return pd.DataFrame(rows)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    EM_FAILS += 1
    raise RuntimeError(f"东财港股{klt}分钟线失败：{type(last).__name__}: {str(last)[:80]}")


# 港股分钟线备用：腾讯当日分时（只有当天，每分钟一个价）→ 自己合成 30/60 分钟线，只补当天
HK_BARS = {
    60: [(930, 1030), (1031, 1130), (1131, 1330), (1331, 1430), (1431, 1530), (1531, 1600)],
    30: [(930, 1000), (1001, 1030), (1031, 1100), (1101, 1130), (1131, 1200), (1201, 1330),
         (1331, 1400), (1401, 1430), (1431, 1500), (1501, 1530), (1531, 1600)],
}


def fetch_hk_today_qq(code):
    num = code.split(".")[0].zfill(5)
    last = None
    for attempt in range(4):
        host = ["web.ifzq.gtimg.cn", "ifzq.gtimg.cn"][attempt % 2]
        try:
            with urlopen(Request(f"https://{host}/appstock/app/hkMinute/query?code=hk{num}",
                                 headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"}), timeout=20) as r:
                js = json.loads(r.read().decode("utf-8", "replace"))
            node = (((js.get("data") or {}).get(f"hk{num}") or {}).get("data") or {})
            rows = []
            for line in node.get("data") or []:
                a = line.split()
                if len(a) >= 2:
                    rows.append((int(a[0]), float(a[1])))
            if not rows:
                raise RuntimeError("当日分时为空")
            return rows, str(node.get("date") or "")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"腾讯港股当日分时失败：{type(last).__name__}: {str(last)[:80]}")


def hk_today_bars(rows, day, period):
    """rows: [(HHMM, 价格)] → [(dt, open, high, low, close)]；收盘前数据不全时返回空"""
    if not rows or rows[-1][0] < 1600:
        return []
    out = []
    for a, b in HK_BARS[period]:
        px = [p for t, p in rows if a <= t <= b]
        if px:
            out.append((f"{day} {b // 100:02d}:{b % 100:02d}", px[0], max(px), min(px), px[-1]))
    return out


def min_db():
    os.makedirs(os.path.dirname(MIN_DB), exist_ok=True)
    con = sqlite3.connect(MIN_DB)
    con.execute("CREATE TABLE IF NOT EXISTS bars(code TEXT, period INT, dt TEXT, open REAL, high REAL, low REAL,"
                " close REAL, PRIMARY KEY(code, period, dt))")
    return con


def minute_mas(code, daily, con):
    """返回 {'m60': {...}, 'm30': {...}}：每个周期的 MA55、MA233（前复权）、根数、来源、状态"""
    out = {}
    today_cache = {}                     # 腾讯当日分时，60/30 两个周期共用一次请求
    for p in (60, 30):
        info = {"ma55": None, "ma233": None, "bars": 0, "src": "", "err": ""}
        try:
            if code.endswith(".HK"):
                info["src"] = "东方财富（逐日积累）"
                try:
                    new = fetch_hk_min_em(code, p)
                    for day in sorted({x[:10] for x in new["dt"]}):      # 东财的数据为准：同一天先删后写
                        con.execute("DELETE FROM bars WHERE code=? AND period=? AND substr(dt,1,10)=?", (code, p, day))
                    con.executemany("INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?)",
                                    [(code, p, r.dt, r.open, r.high, r.low, r.close) for r in new.itertuples()])
                    con.commit()
                except Exception as e:  # noqa: BLE001
                    info["err"] = str(e)[:80]
                    day = daily["date"].iloc[-1].strftime("%Y-%m-%d")          # 最近一个交易日
                    have = con.execute("SELECT COUNT(*) FROM bars WHERE code=? AND period=? AND substr(dt,1,10)=?",
                                       (code, p, day)).fetchone()[0]
                    if have == 0:
                        try:
                            if "qq" not in today_cache:
                                today_cache["qq"] = fetch_hk_today_qq(code)
                            rows, qday = today_cache["qq"]
                            if qday and qday.replace("-", "")[:8] != day.replace("-", ""):
                                raise RuntimeError(f"腾讯分时日期 {qday} 与最近交易日 {day} 不一致")
                            bars = hk_today_bars(rows, day, p)
                            if bars:
                                con.executemany("INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?)",
                                                [(code, p) + b for b in bars])
                                con.commit()
                                info["src"] = "东方财富（逐日积累）+ 今天用腾讯分时补"
                                info["err"] = ""
                        except Exception as e2:  # noqa: BLE001
                            info["err"] = (info["err"] + "；备用也失败：" + str(e2)[:60])[:150]
                    else:
                        info["err"] = ""          # 今天的数据已经有了（之前存过），不算失败
                m = pd.read_sql_query("SELECT dt,open,high,low,close FROM bars WHERE code=? AND period=? ORDER BY dt",
                                      con, params=(code, p))
                m["dt"] = pd.to_datetime(m["dt"])
            else:
                info["src"] = "新浪"
                m = TX.fetch_min_sina(code, p)
            if m.empty:
                raise RuntimeError(info["err"] or "没有分钟线")
            m["date"] = m["dt"].dt.normalize()
            q = TX.to_qfq(m, daily)
            info["bars"] = int(len(q))
            c = q["close"]
            if len(c) >= 55:
                info["ma55"] = f(c.tail(55).mean())
            if len(c) >= 233:
                info["ma233"] = f(c.tail(233).mean())
        except Exception as e:  # noqa: BLE001
            info["err"] = info["err"] or str(e)[:80]
        out[f"m{p}"] = info
    return out


# ---------------- 历史统计 ----------------
def hist_stats(d, trend, side, hits):
    col = "buy" if side == "正T" else "sell"
    m = (d["trend"] == trend) & d["has_next"] & d["sup"].notna() & d["res"].notna()
    if hits > 0:
        m &= d[f"{col}_hits"] >= hits
    s = d[m]
    n = int(len(s))
    if n == 0:
        return {"n": 0}
    fl = s[f"{col}_fill"]
    nf = int(fl.sum())
    res = {"n": n, "fill": f(nf / n, 4), "fill2": f(s[f"{col}2_fill"].mean(), 4), "nf": nf}
    for k in ("cons", "d1", "d2", "d3"):
        res[k] = f(s.loc[fl, f"{col}_{k}"].mean(), 4) if nf else None
    res["dd3"] = f(s.loc[fl, f"{col}_dd3"].mean(), 4) if nf else None
    return res


# ---------------- 单只股票 ----------------
def analyze(code, name, raw, con):
    d = R.compute(raw, version=VER)
    d = BT.evaluate(d)
    d = BT.evaluate(d, "sup2", "res2", "buy2", "sell2")
    last = d.iloc[-1]
    prev = d.iloc[-2] if len(d) > 1 else last
    mins = minute_mas(code, raw, con)
    extra = {}
    for p, lab in (("m60", "60分钟"), ("m30", "30分钟")):
        for k in ("ma55", "ma233"):
            v = mins[p][k]
            if v is not None:
                extra[f"{lab}{k.upper()}"] = v
    c = float(last["close"])
    s1, s1src, r1, r1src = R.levels_row(last, version=1)
    s2, s2src, r2, r2src = R.levels_row(last, version=2)

    def lvl(v, src):
        if v is None or not np.isfinite(v):
            return None
        near = [k for k, x in extra.items() if abs(x - v) / v <= R.P["strong_band"]]
        allsrc = list(src) + near
        return {"price": f(v, 3), "dist": f((v / c - 1) * 100, 2), "src": allsrc, "strong": len(allsrc) >= 2}

    ladder = []
    for k, lab in R.LEVEL_COLS.items():
        v = last.get(k)
        if pd.notna(v):
            ladder.append({"name": lab, "price": f(v, 3), "dist": f((v / c - 1) * 100, 2), "tf": "日线"})
    for k, v in extra.items():
        ladder.append({"name": k, "price": f(v, 3), "dist": f((v / c - 1) * 100, 2), "tf": k[:4]})
    ladder.sort(key=lambda x: -x["price"])

    def pctile(col):
        h = d[col].dropna()
        v = last.get(col)
        return f((h <= v).mean() * 100, 1) if len(h) >= 250 and pd.notna(v) else None

    rng = R.P
    ind = {k: f(last.get(k), 4) for k in ("rsi6", "rsi24", "bias20", "bias55", "k", "d", "j", "dif", "dea", "hist",
                                          "boll_up", "ma20", "boll_low", "vr", "atr", "ma5", "ma10", "ma55", "ma233")}
    ind["hist_prev"] = f(prev.get("hist"), 4)
    ind["bias20_pct"] = pctile("bias20")
    ind["bias55_pct"] = pctile("bias55")
    bu, bl = last.get("boll_up"), last.get("boll_low")
    ind["pctb"] = f((c - bl) / (bu - bl) * 100, 1) if pd.notna(bu) and pd.notna(bl) and bu > bl else None

    trend = last["trend"]
    sides = {}
    stats = {}
    ks = {}
    for (tr, sd), conds in R.RULES.items():
        n = sum(1 for x in conds if R.is_core(x))
        ks[(tr, sd)] = max(1, int(np.ceil(0.6 * n)))
    for sd, col in (("正T", "buy"), ("反T", "sell")):
        if trend not in ("上涨", "下跌", "震荡"):
            sides[sd] = None
            continue
        conds = [x for x in R.RULES[(trend, sd)] if R.is_core(x)]
        lst = [{"desc": x[1], "ok": bool(last.get(f"c_{trend}_{sd}_{x[0]}", False))} for x in conds]
        hits = sum(1 for x in lst if x["ok"])
        k = ks[(trend, sd)]
        tier = "强" if hits >= k else ("一般" if hits >= max(1, k - 1) else "")
        sides[sd] = {"hits": hits, "n": len(lst), "k": k, "tier": tier, "conds": lst}
        stats[sd] = {"own": hist_stats(d.iloc[:-1], trend, sd, hits), "hits": hits}
    out = {
        "code": code, "name": name, "market": "HK" if code.endswith(".HK") else "A",
        "date": last["date"].strftime("%Y-%m-%d"), "close": f(c, 3), "pct": f(last.get("pct"), 2),
        "trend": trend, "long": last.get("long"), "bars": int(len(raw)),
        "atr": f(last.get("atr"), 3), "atr_pct": f(last.get("atr") / c * 100, 2) if pd.notna(last.get("atr")) else None,
        "range_lo": f(c - 0.7 * last.get("atr"), 3) if pd.notna(last.get("atr")) else None,
        "range_hi": f(c + 0.7 * last.get("atr"), 3) if pd.notna(last.get("atr")) else None,
        "sup1": lvl(s1, s1src), "res1": lvl(r1, r1src), "sup2": lvl(s2, s2src), "res2": lvl(r2, r2src),
        "ladder": ladder, "minute": mins, "ind": ind, "sides": sides, "stats": stats,
    }
    tail = d.tail(160)
    kline = {"code": code, "name": name,
             "dates": tail["date"].dt.strftime("%Y-%m-%d").tolist(),
             "ohlc": [[f(r.open, 3), f(r.close, 3), f(r.low, 3), f(r.high, 3)] for r in tail.itertuples()],
             "vol": [f(v, 0) for v in tail["vol"]],
             "ma20": [f(v, 3) for v in tail["ma20"]], "ma55": [f(v, 3) for v in tail["ma55"]],
             "ma233": [f(v, 3) for v in tail["ma233"]],
             "boll_up": [f(v, 3) for v in tail["boll_up"]], "boll_low": [f(v, 3) for v in tail["boll_low"]]}
    return out, kline, d


def main():
    demo = "--demo" in sys.argv
    end = date.today()
    start = end - timedelta(days=round(365.25 * YEARS) + 400)
    s8, e8 = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    stocks = load_focus() if not demo else [("600000.SH", "演示A"), ("00700.HK", "演示港")]
    print(f"T助手：重点股 {len(stocks)} 只，取 {start} → {end} 前复权日线 …", flush=True)
    os.makedirs(os.path.join(OUT, "kline"), exist_ok=True)
    con = min_db()
    items, frames, fails = [], {}, []
    for code, name in stocks:
        try:
            raw = BT.fetch_demo(code, s8, e8) if demo else (BT.fetch_hk(code, s8, e8) if code.endswith(".HK")
                                                            else BT.fetch_a(code, s8, e8))
            if raw is None or len(raw) < 60:
                raise RuntimeError(f"日线太少（{0 if raw is None else len(raw)} 根）")
            if demo:
                TX.fetch_min_sina = lambda c, p=60, _r=raw: TX.demo_minutes(_r).assign(
                    close=lambda x: x["close"])   # noqa: E731
                globals()["fetch_hk_min_em"] = lambda c, k, _r=raw: TX.demo_minutes(_r).assign(
                    dt=lambda x: x["dt"].dt.strftime("%Y-%m-%d %H:%M"))[["dt", "open", "close", "high", "low"]]
            it, kl, d = analyze(code, name, raw, con)
            items.append(it)
            frames[code] = d
            with open(os.path.join(OUT, "kline", code + ".json"), "w", encoding="utf-8") as fh:
                json.dump(kl, fh, ensure_ascii=False, separators=(",", ":"))
            m = it["minute"]
            print(f"  ✓ {code} {name}  {it['date']} 收 {it['close']}  {it['trend']}  "
                  f"60分钟{m['m60']['bars']}根 30分钟{m['m30']['bars']}根"
                  + (f"  ⚠ {m['m60']['err'] or m['m30']['err']}" if (m['m60']['err'] or m['m30']['err']) else ""),
                  flush=True)
        except Exception as e:  # noqa: BLE001
            fails.append({"code": code, "name": name, "err": f"{type(e).__name__}: {str(e)[:150]}"})
            print(f"  ✗ {code} {name}  {type(e).__name__}: {str(e)[:150]}", flush=True)
            traceback.print_exc(limit=1)
    con.close()

    # 全部重点股合并的历史统计（同趋势、同侧、条件数 ≥ 今天）
    if frames:
        pool = pd.concat([d.iloc[:-1] for d in frames.values()], ignore_index=True)
        cache = {}
        for it in items:
            for sd in ("正T", "反T"):
                st = it["stats"].get(sd)
                if not st:
                    continue
                key = (it["trend"], sd, st["hits"])
                if key not in cache:
                    cache[key] = hist_stats(pool, it["trend"], sd, st["hits"])
                st["pool"] = cache[key]
    meta = {"schema_version": 1, "generated_at": datetime.now(CN_TZ).isoformat(timespec="minutes"),
            "rule_version": VER, "years": YEARS, "notes": IND_NOTE,
            "level_params": {"min_gap": R.P["min_gap"], "level_min_atr": R.P["level_min_atr"],
                             "strong_band": R.P["strong_band"]},
            "stocks": items, "fails": fails}
    with open(os.path.join(OUT, "overview.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"\n完成：{len(items)} 只，失败 {len(fails)} 只 → {OUT}/overview.json")
    if not items:
        sys.exit(1)


if __name__ == "__main__":
    main()
