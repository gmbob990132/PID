#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
做T 第二步：用重点股的历史日线回测规则

对每只"重点"股票（tech_config/stocks.csv 里 focus=1）取近 N 年前复权日线
（A 股 Tushare，港股腾讯，和技术指标模块同一来源），逐日按 t_rules 计算：
  趋势、支撑/压力、正T / 反T 各条件是否满足
再用"第二天"的开高低收检验：

  正T（先买后卖）：第二天最低价 ≤ 支撑 → 视为在支撑买到（若开盘就低于支撑，按开盘价）
      乐观成功：当天最高价 ≥ 买价 × (1+1.5%)      （日线看不出先后，偏乐观）
      保守成功：当天收盘价 ≥ 买价 × (1+0.5%)      （至少收盘能卖出赚钱）
  反T（先卖后买）：第二天最高价 ≥ 压力 → 视为在压力卖出（若开盘就高于压力，按开盘价）
      乐观成功：当天最低价 ≤ 卖价 × (1-1.5%)
      保守成功：当天收盘价 ≤ 卖价 × (1-0.5%)

  跨日窗口（买入/卖出那天之后的 1/2/3 个交易日内，不含成交当天，因此不受日线先后顺序影响）：
      正T：之后 N 天内最高价 ≥ 买价 × (1+1.5%)     反T：之后 N 天内最低价 ≤ 卖价 × (1-1.5%)
      3日最大不利：正T = 之后 3 天最低价相对买价的最大跌幅；反T = 之后 3 天最高价相对卖价的最大涨幅

第一版 / 第二版规则并排输出（第二版：删掉无效条件、补有效条件，支撑压力至少离收盘 0.5 个 ATR）。

输出（打印到日志 + 存 CSV，工作流会作为附件上传）：
  1) 趋势分布、正T/反T 条件同时命中的频率
  2) 每个 趋势×方向：按命中条数分档的成交率、成功率，以及"不看条件"的基准
  3) 每条条件的增益：满足时 vs 不满足时的保守成功率
用法：
  python3 t_backtest.py                 # 联网
  python3 t_backtest.py --years 5
  python3 t_backtest.py --demo          # 合成数据，离线自检
"""
import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

import t_rules as R

HERE = os.path.dirname(os.path.abspath(__file__))
STOCKS_CSV = os.path.join(HERE, "tech_config", "stocks.csv")
OUT_DIR = os.path.join(HERE, "t_backtest_out")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
GAIN_OPT, GAIN_CONS = 0.015, 0.005


# ---------------- 数据 ----------------
def load_focus():
    df = pd.read_csv(STOCKS_CSV, dtype=str, encoding="utf-8-sig").fillna("")
    df = df[df["focus"].str.strip() == "1"]
    return list(zip(df["code"].str.strip(), df["name"].str.strip()))


def fetch_a(code, start, end):
    import tushare as ts
    from tech_ingest import ts_call            # 复用技术指标模块的限速+重试
    ts.set_token(os.environ.get("TUSHARE_TOKEN", ""))
    df = ts_call(ts.pro_bar, ts_code=code, adj="qfq", start_date=start, end_date=end, asset="E", freq="D")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"trade_date": "date"})
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    return df[["date", "open", "high", "low", "close", "vol"]].sort_values("date").reset_index(drop=True)


def fetch_hk(code, start, end):
    bare = code.split(".")[0].lstrip("0").zfill(5)
    d0 = pd.Timestamp(start).date()
    d1 = pd.Timestamp(end).date()
    rows, cur = [], d0
    while cur <= d1:
        nxt = min(cur + timedelta(days=900), d1)
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=hk{bare},day,"
               f"{cur.isoformat()},{nxt.isoformat()},800,qfq")
        for attempt in range(3):
            try:
                with urlopen(Request(url, headers={"User-Agent": UA}), timeout=20) as r:
                    js = json.loads(r.read().decode("utf-8", "replace"))
                break
            except Exception:  # noqa: BLE001
                if attempt == 2:
                    raise
                time.sleep(3)
        node = (js.get("data") or {}).get(f"hk{bare}") or {}
        for x in node.get("qfqday") or node.get("day") or []:
            if len(x) >= 6:
                rows.append({"date": pd.to_datetime(x[0]), "open": float(x[1]), "close": float(x[2]),
                             "high": float(x[3]), "low": float(x[4]), "vol": float(x[5])})
        cur = nxt + timedelta(days=1)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).drop_duplicates("date").sort_values("date").reset_index(drop=True)


def fetch_demo(code, start, end):
    rnd = np.random.default_rng(abs(hash(code)) % 2**32)
    days = pd.bdate_range(start, end)
    ret = rnd.normal(0.0003, 0.02, len(days))
    c = 30 * np.exp(np.cumsum(ret))
    o = c * (1 + rnd.normal(0, 0.006, len(days)))
    h = np.maximum(o, c) * (1 + np.abs(rnd.normal(0, 0.01, len(days))))
    l = np.minimum(o, c) * (1 - np.abs(rnd.normal(0, 0.01, len(days))))
    v = rnd.lognormal(15, 0.4, len(days))
    return pd.DataFrame({"date": days, "open": o, "high": h, "low": l, "close": c, "vol": v})


# ---------------- 检验第二天 ----------------
def evaluate(d):
    n1 = d.shift(-1)
    o1, h1, l1, c1 = n1["open"], n1["high"], n1["low"], n1["close"]
    hs = [d["high"].shift(-k) for k in (2, 3, 4)]      # 成交日之后第 1/2/3 天
    ls = [d["low"].shift(-k) for k in (2, 3, 4)]
    s, r = d["sup"], d["res"]
    buy_px = pd.Series(np.where(o1 < s, o1, s), index=d.index)
    d["buy_fill"] = (l1 <= s) & o1.notna()
    d["buy_opt"] = d["buy_fill"] & (h1 >= buy_px * (1 + GAIN_OPT))
    d["buy_cons"] = d["buy_fill"] & (c1 >= buy_px * (1 + GAIN_CONS))
    d["buy_ret"] = np.where(d["buy_fill"], c1 / buy_px - 1, np.nan)
    sell_px = pd.Series(np.where(o1 > r, o1, r), index=d.index)
    d["sell_fill"] = (h1 >= r) & o1.notna()
    d["sell_opt"] = d["sell_fill"] & (l1 <= sell_px * (1 - GAIN_OPT))
    d["sell_cons"] = d["sell_fill"] & (c1 <= sell_px * (1 - GAIN_CONS))
    d["sell_ret"] = np.where(d["sell_fill"], 1 - c1 / sell_px, np.nan)
    for n in (1, 2, 3):
        hmax = pd.concat(hs[:n], axis=1).max(axis=1, skipna=False)
        lmin = pd.concat(ls[:n], axis=1).min(axis=1, skipna=False)
        d[f"buy_d{n}"] = d["buy_fill"] & (hmax >= buy_px * (1 + GAIN_OPT))
        d[f"sell_d{n}"] = d["sell_fill"] & (lmin <= sell_px * (1 - GAIN_OPT))
    l3 = pd.concat(ls, axis=1).min(axis=1, skipna=False)
    h3 = pd.concat(hs, axis=1).max(axis=1, skipna=False)
    d["buy_dd3"] = np.where(d["buy_fill"], np.minimum(l3 / buy_px - 1, 0), np.nan)
    d["sell_dd3"] = np.where(d["sell_fill"], np.maximum(h3 / sell_px - 1, 0), np.nan)
    d["has_next"] = hs[2].notna()          # 需要完整的 3 天窗口
    return d


# ---------------- 报告 ----------------
def pct(x):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:5.1f}%"


def block(sub, col):
    """sub: 某一组的行；col: buy/sell"""
    n = len(sub)
    out = dict(n=n)
    f = sub[f"{col}_fill"] if n else None
    nf = int(f.sum()) if n else 0
    out["fill"] = nf / n if n else np.nan
    for k in ("opt", "cons", "d1", "d2", "d3"):
        out[k] = (sub[f"{col}_{k}"].sum() / nf) if nf else np.nan
    out["ret"] = sub.loc[f, f"{col}_ret"].mean() if nf else np.nan
    out["dd3"] = sub.loc[f, f"{col}_dd3"].mean() if nf else np.nan
    return out


HEAD = (f"  {'命中':<6}{'天数':>7}{'成交率':>8}{'当日乐观':>9}{'当日保守':>9}"
        f"{'1日内':>8}{'2日内':>8}{'3日内':>8}{'3日最大不利':>11}")


def row_fmt(label, b):
    return (f"  {label:<8}{b['n']:>7}{pct(b['fill']):>8}{pct(b['opt']):>9}{pct(b['cons']):>9}"
            f"{pct(b['d1']):>8}{pct(b['d2']):>8}{pct(b['d3']):>8}{pct(b['dd3']):>11}")


def report(all_d, lines, ver):
    def p(s=""):
        print(s)
        lines.append(s)

    rules = R.RULESETS[ver]
    d = all_d[all_d["has_next"] & all_d["trend"].notna() & all_d["sup"].notna() & all_d["res"].notna()]
    p(f"\n{'=' * 30}  第 {ver} 版规则  {'=' * 30}")
    p(f"样本：{d['code'].nunique()} 只股票，{len(d)} 个交易日"
      f"（{d['date'].min():%Y-%m-%d} → {d['date'].max():%Y-%m-%d}）")
    p()
    p("## 1. 趋势分布与正/反T 同时命中")
    for tr in ("上涨", "下跌", "震荡"):
        s = d[d["trend"] == tr]
        if s.empty:
            continue
        both = ((s["buy_ratio"] >= 0.5) & (s["sell_ratio"] >= 0.5)).mean()
        unclear = ((s["buy_hits"] >= 2) & (s["sell_hits"] >= 2) & ((s["buy_hits"] - s["sell_hits"]).abs() <= 1)).mean()
        p(f"  {tr}：{len(s):>6} 天（{len(s) / len(d) * 100:4.1f}%）  "
          f"正/反T 都命中一半以上：{pct(both)}   方向不明（两边都≥2条且相差≤1）：{pct(unclear)}")
    p(f"  支撑到压力空间 < 1.5% 的天数占比：{pct((d['space'] < R.P['space_min']).mean())}"
      f"   空间中位数：{pct(d['space'].median())}")
    p()
    p("## 2. 各趋势 × 方向：按命中条数分档")
    p("  当日乐观/保守：成交当天（日线看不出先后，乐观偏高）；1/2/3日内：成交后 N 个交易日内走出 1.5% 价差；")
    p("  3日最大不利：成交后 3 天内价格朝不利方向的平均最大幅度（正T 为跌幅，反T 为涨幅）。")
    for (tr, side), conds in rules.items():
        col = "buy" if side == "正T" else "sell"
        s = d[d["trend"] == tr]
        if s.empty:
            continue
        p(f"\n  ### {tr}趋势 · {side}（共 {len(conds)} 条）")
        p(HEAD)
        p(row_fmt("基准", block(s, col)))
        for k in range(len(conds) + 1):
            g = s[s[f"{col}_hits"] == k]
            if len(g) < 5:
                continue
            p(row_fmt(str(k), block(g, col)))
    p()
    p("## 3. 单条条件的增益（满足 vs 不满足；只统计该趋势下成交了的日子）")
    p("  左边看当日保守成功率，右边看 3 日内走出 1.5% 价差的比例")
    for (tr, side), conds in rules.items():
        col = "buy" if side == "正T" else "sell"
        s = d[(d["trend"] == tr) & d[f"{col}_fill"]]
        if s.empty:
            continue
        p(f"\n  ### {tr} · {side}")
        for code, desc, _ in conds:
            m = s[f"c_{tr}_{side}_{code}"].fillna(False).astype(bool)
            yes, no = s[m], s[~m]
            parts = []
            for k in ("cons", "d3"):
                ry = yes[f"{col}_{k}"].mean() if len(yes) else np.nan
                rn = no[f"{col}_{k}"].mean() if len(no) else np.nan
                lift = (ry - rn) * 100 if len(yes) and len(no) else np.nan
                parts.append(f"{pct(ry)} vs {pct(rn)} ({'—' if np.isnan(lift) else f'{lift:+.1f}pt'})")
            p(f"  {desc:<30} 满足 {len(yes):>5} 次 | 当日保守 {parts[0]} | 3日内 {parts[1]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=10)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--only", help="只跑这些代码，逗号分隔")
    a = ap.parse_args()

    end = date.today()
    start = end - timedelta(days=round(365.25 * a.years) + 400)     # 多取约 400 天给 MA233/分位预热
    s8, e8 = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    stocks = load_focus() if not a.demo else [("600000.SH", "演示A"), ("00700.HK", "演示港")]
    if a.only:
        want = set(a.only.split(","))
        stocks = [x for x in stocks if x[0] in want]
    print(f"重点股 {len(stocks)} 只，取 {start} → {end} 前复权日线 …\n")

    frames, fails = [], []
    for code, name in stocks:
        try:
            raw = fetch_demo(code, s8, e8) if a.demo else (fetch_hk(code, s8, e8) if code.endswith(".HK")
                                                           else fetch_a(code, s8, e8))
            if raw is None or len(raw) < 300:
                raise RuntimeError(f"数据太少（{0 if raw is None else len(raw)} 根）")
            ind = R.add_trend(R.add_indicators(raw))
            cut = pd.Timestamp(end - timedelta(days=round(365.25 * a.years)))
            for ver in (1, 2):
                d = evaluate(R.compute(raw, version=ver, ind=ind))
                d = d[d["date"] >= cut].copy()
                d["code"], d["name"], d["ver"] = code, name, ver
                frames.append(d)
            print(f"  ✓ {code} {name}  {len(raw)} 根，回测 {len(d)} 天")
        except Exception as e:  # noqa: BLE001
            fails.append(code)
            print(f"  ✗ {code} {name}  {type(e).__name__}: {str(e)[:120]}")
    if not frames:
        print("没有任何股票取到数据")
        sys.exit(1)
    all_d = pd.concat(frames, ignore_index=True)

    os.makedirs(OUT_DIR, exist_ok=True)
    lines = ["成功口径：当日乐观 = 成交当天最高/最低价走出 1.5% 价差；当日保守 = 成交当天收盘仍有 0.5% 以上利润；",
             "          N日内 = 成交后 N 个交易日内（不含成交当天）走出 1.5% 价差。成功率均以成交了的为分母。"]
    print("\n".join(lines))
    for ver in (1, 2):
        report(all_d[all_d["ver"] == ver], lines, ver)
    if fails:
        lines.append(f"\n取数失败：{', '.join(fails)}")
        print(lines[-1])
    keep = ["ver", "code", "name", "date", "open", "high", "low", "close", "trend", "long", "sup", "res", "pos", "space",
            "buy_hits", "buy_n", "sell_hits", "sell_n", "buy_fill", "buy_opt", "buy_cons", "buy_ret",
            "sell_fill", "sell_opt", "sell_cons", "sell_ret", "buy_d1", "buy_d2", "buy_d3", "sell_d1", "sell_d2", "sell_d3",
            "buy_dd3", "sell_dd3", "rsi6", "rsi24", "bias20", "bias55", "j", "vr"]
    all_d[keep].to_csv(os.path.join(OUT_DIR, "daily_signals.csv"), index=False, encoding="utf-8-sig")
    with open(os.path.join(OUT_DIR, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n明细已存 {OUT_DIR}/daily_signals.csv，报告 {OUT_DIR}/report.txt")


if __name__ == "__main__":
    main()
