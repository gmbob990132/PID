#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
做T 回测（第四版规则）

股票分三组（tech_config/stocks.csv + tech_config/t_holdout.csv）：
  调参组   = 39 只重点股 + 非重点股中除保留集外的其余股票
  保留集   = t_holdout.csv 里随机抽出的 30 只非重点股：平时完全不看，只有加 --final 才计算和输出
时间切分：2016–2022 = 调参段；2023 以后 = 检验段

规则：t_rules.RULES_V4（第三版的条件不变，只调整了核心/候选）。命中数只数"核心"条件；"候选"条件照样计算、单独报告增益，等数据多了再定去留。
支撑/压力两档：第一档 = 离收盘最近的价位（≥0.3%）；第二档 = 至少离收盘 0.5 个 ATR。

检验口径（成交以"第二天"价格为准）：
  正T：第二天最低价 ≤ 支撑 → 买到（开盘就低于支撑按开盘价）；反T：第二天最高价 ≥ 压力 → 卖出
  当日乐观：成交当天走出 1.5% 价差（日线看不出先后，偏乐观）；当日保守：成交当天收盘仍有 0.5% 以上利润
  N日内：成交后 N 个交易日内（不含成交当天）走出 1.5% 价差
  3日最大不利：成交后 3 天内价格朝不利方向的平均最大幅度
用法：
  python3 t_backtest.py                 # 调参组：调参段详表 + 检验段汇总
  python3 t_backtest.py --final         # 另外计算保留集（只在最后检验时用）
  python3 t_backtest.py --demo          # 合成数据离线自检
"""
import argparse
import json
import math
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
HOLDOUT_CSV = os.path.join(HERE, "tech_config", "t_holdout.csv")
OUT_DIR = os.path.join(HERE, "t_backtest_out")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
GAIN_OPT, GAIN_CONS = 0.015, 0.005
SPLIT = pd.Timestamp("2023-01-01")
VER = 4


# ---------------- 数据 ----------------
def load_universe(final=False):
    """返回 [(代码, 名称, 组别)]；组别 = 重点 / 非重点 / 保留集"""
    df = pd.read_csv(STOCKS_CSV, dtype=str, encoding="utf-8-sig").fillna("")
    hold = set()
    if os.path.exists(HOLDOUT_CSV):
        h = pd.read_csv(HOLDOUT_CSV, dtype=str, encoding="utf-8-sig", comment="#").fillna("")
        hold = set(h["code"].str.strip())
    out = []
    for _, r in df.iterrows():
        code, name = r["code"].strip(), r["name"].strip()
        if not code:
            continue
        if code in hold:
            if final:
                out.append((code, name, "保留集"))
            continue
        out.append((code, name, "重点" if r["focus"].strip() == "1" else "非重点"))
    return out, len(hold)


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
    """腾讯港股前复权日线。两个域名轮流试、失败退避重试（Actions 上偶发 DNS 解析失败/限流）"""
    bare = code.split(".")[0].lstrip("0").zfill(5)
    d0 = pd.Timestamp(start).date()
    d1 = pd.Timestamp(end).date()
    rows, cur = [], d0
    hosts = ["web.ifzq.gtimg.cn", "ifzq.gtimg.cn"]
    while cur <= d1:
        nxt = min(cur + timedelta(days=900), d1)
        js, last = None, None
        for attempt in range(6):
            host = hosts[attempt % 2]
            url = (f"https://{host}/appstock/app/fqkline/get?param=hk{bare},day,"
                   f"{cur.isoformat()},{nxt.isoformat()},800,qfq")
            try:
                with urlopen(Request(url, headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"}),
                             timeout=20) as r:
                    js = json.loads(r.read().decode("utf-8", "replace"))
                if js.get("code") not in (0, "0", None):
                    raise RuntimeError(f"腾讯返回 code={js.get('code')} msg={js.get('msg')}")
                break
            except Exception as e:  # noqa: BLE001
                last, js = e, None
                time.sleep(2 * (attempt + 1))
        if js is None:
            raise RuntimeError(f"腾讯港股日线 6 次都失败：{type(last).__name__}: {str(last)[:100]}")
        node = (js.get("data") or {}).get(f"hk{bare}") or {}
        for x in node.get("qfqday") or node.get("day") or []:
            if len(x) >= 6:
                rows.append({"date": pd.to_datetime(x[0]), "open": float(x[1]), "close": float(x[2]),
                             "high": float(x[3]), "low": float(x[4]), "vol": float(x[5])})
        cur = nxt + timedelta(days=1)
        time.sleep(0.5)
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
def evaluate(d, sup="sup", res="res", bp="buy", sp="sell"):
    n1 = d.shift(-1)
    o1, h1, l1, c1 = n1["open"], n1["high"], n1["low"], n1["close"]
    hs = [d["high"].shift(-k) for k in (2, 3, 4)]
    ls = [d["low"].shift(-k) for k in (2, 3, 4)]
    s, r = d[sup], d[res]
    buy_px = pd.Series(np.where(o1 < s, o1, s), index=d.index)
    d[f"{bp}_fill"] = (l1 <= s) & o1.notna()
    d[f"{bp}_opt"] = d[f"{bp}_fill"] & (h1 >= buy_px * (1 + GAIN_OPT))
    d[f"{bp}_cons"] = d[f"{bp}_fill"] & (c1 >= buy_px * (1 + GAIN_CONS))
    d[f"{bp}_ret"] = np.where(d[f"{bp}_fill"], c1 / buy_px - 1, np.nan)
    sell_px = pd.Series(np.where(o1 > r, o1, r), index=d.index)
    d[f"{sp}_fill"] = (h1 >= r) & o1.notna()
    d[f"{sp}_opt"] = d[f"{sp}_fill"] & (l1 <= sell_px * (1 - GAIN_OPT))
    d[f"{sp}_cons"] = d[f"{sp}_fill"] & (c1 <= sell_px * (1 - GAIN_CONS))
    d[f"{sp}_ret"] = np.where(d[f"{sp}_fill"], 1 - c1 / sell_px, np.nan)
    for n in (1, 2, 3):
        hmax = pd.concat(hs[:n], axis=1).max(axis=1, skipna=False)
        lmin = pd.concat(ls[:n], axis=1).min(axis=1, skipna=False)
        d[f"{bp}_d{n}"] = d[f"{bp}_fill"] & (hmax >= buy_px * (1 + GAIN_OPT))
        d[f"{sp}_d{n}"] = d[f"{sp}_fill"] & (lmin <= sell_px * (1 - GAIN_OPT))
    l3 = pd.concat(ls, axis=1).min(axis=1, skipna=False)
    h3 = pd.concat(hs, axis=1).max(axis=1, skipna=False)
    d[f"{bp}_dd3"] = np.where(d[f"{bp}_fill"], np.minimum(l3 / buy_px - 1, 0), np.nan)
    d[f"{sp}_dd3"] = np.where(d[f"{sp}_fill"], np.maximum(h3 / sell_px - 1, 0), np.nan)
    d["has_next"] = hs[2].notna()
    return d


# ---------------- 报告 ----------------
def pct(x):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:5.1f}%"


def block(sub, col):
    n = len(sub)
    out = dict(n=n)
    nf = int(sub[f"{col}_fill"].sum()) if n else 0
    out["nf"] = nf
    out["fill"] = nf / n if n else np.nan
    for k in ("opt", "cons", "d1", "d2", "d3"):
        out[k] = (sub[f"{col}_{k}"].sum() / nf) if nf else np.nan
    f = sub[f"{col}_fill"] if n else None
    out["dd3"] = sub.loc[f, f"{col}_dd3"].mean() if nf else np.nan
    return out


def k_threshold(conds):
    n = sum(1 for c in conds if R.is_core(c))
    return max(1, math.ceil(0.6 * n))


HEAD = (f"  {'命中':<6}{'天数':>7}{'成交率':>8}{'当日乐观':>9}{'当日保守':>9}{'1日内':>8}{'2日内':>8}"
        f"{'3日内':>8}{'3日不利':>8} │{'二档成交':>8}{'二档保守':>8}{'二档3日':>8}")


def row_fmt(label, s, col):
    b, b2 = block(s, col), block(s, col.replace("buy", "buy2").replace("sell", "sell2"))
    return (f"  {label:<8}{b['n']:>7}{pct(b['fill']):>8}{pct(b['opt']):>9}{pct(b['cons']):>9}{pct(b['d1']):>8}"
            f"{pct(b['d2']):>8}{pct(b['d3']):>8}{pct(b['dd3']):>8} │{pct(b2['fill']):>8}{pct(b2['cons']):>8}"
            f"{pct(b2['d3']):>8}")


def valid(d):
    return d[d["has_next"] & d["trend"].notna() & d["sup"].notna() & d["res"].notna()]


def report(all_d, lines, final):
    def p(s=""):
        print(s)
        lines.append(s)

    rules = R.RULESETS[VER]
    d = valid(all_d)
    tune = d[(d["grp"] != "保留集") & (d["date"] < SPLIT)]

    p("## 0. 样本")
    for (g, per), sub in d.groupby(["grp", d["date"] >= SPLIT]):
        p(f"  {g:<4} {'2023 以后' if per else '2016–2022'}：{sub['code'].nunique():>3} 只，{len(sub):>6} 个交易日")
    p()
    p("## 1. 调参段（调参组 × 2016–2022）：趋势分布、同时命中、空间")
    for tr in ("上涨", "下跌", "震荡"):
        s = tune[tune["trend"] == tr]
        if s.empty:
            continue
        unclear = ((s["buy_hits"] >= 2) & (s["sell_hits"] >= 2) & ((s["buy_hits"] - s["sell_hits"]).abs() <= 1)).mean()
        p(f"  {tr}：{len(s):>6} 天（{len(s) / len(tune) * 100:4.1f}%）   方向不明（两边核心条件都≥2条且相差≤1）：{pct(unclear)}")
    sp1 = (tune["res"] - tune["sup"]) / tune["close"]
    sp2 = (tune["res2"] - tune["sup2"]) / tune["close"]
    p(f"  第一档空间中位数 {pct(sp1.median())}（<1.5% 占 {pct((sp1 < 0.015).mean())}）；第二档空间中位数 {pct(sp2.median())}")
    p()
    p("## 2. 调参段：各趋势 × 方向，按核心条件命中数累计分档（≥k 条）")
    p("  左边是第一档（最近）支撑压力；│ 右边是第二档（≥0.5 ATR）")
    for (tr, side), conds in rules.items():
        col = "buy" if side == "正T" else "sell"
        s = tune[tune["trend"] == tr]
        if s.empty:
            continue
        ncore = sum(1 for c in conds if R.is_core(c))
        p(f"\n  ### {tr} · {side}（核心 {ncore} 条，提示门槛 ≥{k_threshold(conds)} 条）")
        p(HEAD)
        p(row_fmt("基准", s, col))
        for k in range(1, ncore + 1):
            g = s[s[f"{col}_hits"] >= k]
            if len(g) >= 5:
                p(row_fmt(f"≥{k}", g, col))
    p()
    p("## 3. 调参段：单条条件增益（★核心 / ☆候选；满足 vs 不满足，只看第一档成交了的日子）")
    for (tr, side), conds in rules.items():
        col = "buy" if side == "正T" else "sell"
        s = tune[(tune["trend"] == tr) & tune[f"{col}_fill"]]
        if s.empty:
            continue
        p(f"\n  ### {tr} · {side}")
        for c in conds:
            m = s[f"c_{tr}_{side}_{c[0]}"].fillna(False).astype(bool)
            yes, no = s[m], s[~m]
            parts = []
            for k in ("cons", "d3"):
                ry = yes[f"{col}_{k}"].mean() if len(yes) else np.nan
                rn = no[f"{col}_{k}"].mean() if len(no) else np.nan
                lift = (ry - rn) * 100 if len(yes) and len(no) else np.nan
                parts.append(f"{pct(ry)} vs {pct(rn)} ({'—' if np.isnan(lift) else f'{lift:+.1f}pt'})")
            star = "★" if R.is_core(c) else "☆"
            p(f"  {star} {c[1]:<30} 满足 {len(yes):>5} 次 | 当日保守 {parts[0]} | 3日内 {parts[1]}")
    p()
    p("## 4. 各段对比：基准 vs 达到提示门槛（第一档；格式 = 当日保守 / 3日内，括号里是成交笔数）")
    segs = [("调参段 全部", tune),
            ("检验段 重点", d[(d["grp"] == "重点") & (d["date"] >= SPLIT)]),
            ("检验段 非重点", d[(d["grp"] == "非重点") & (d["date"] >= SPLIT)])]
    if final:
        segs.append(("保留集 全部年份", d[d["grp"] == "保留集"]))
    for (tr, side), conds in rules.items():
        col = "buy" if side == "正T" else "sell"
        k = k_threshold(conds)
        p(f"\n  ### {tr} · {side}（门槛 ≥{k} 条）")
        for name, seg in segs:
            s = seg[seg["trend"] == tr]
            if s.empty:
                continue
            b0, b1 = block(s, col), block(s[s[f"{col}_hits"] >= k], col)
            p(f"  {name:<12} 基准 {pct(b0['cons'])} / {pct(b0['d3'])} ({b0['nf']:>5})   "
              f"达门槛 {pct(b1['cons'])} / {pct(b1['d3'])} ({b1['nf']:>4})   "
              f"提升 {((b1['cons'] - b0['cons']) * 100) if b1['nf'] else float('nan'):+.1f}pt / "
              f"{((b1['d3'] - b0['d3']) * 100) if b1['nf'] else float('nan'):+.1f}pt")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=10)
    ap.add_argument("--final", action="store_true", help="计算并输出保留集（只在最后检验时用）")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--only", help="只跑这些代码，逗号分隔")
    a = ap.parse_args()

    end = date.today()
    start = end - timedelta(days=round(365.25 * a.years) + 400)
    s8, e8 = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    if a.demo:
        stocks, nh = [("600000.SH", "演示A", "重点"), ("00700.HK", "演示港", "非重点"),
                      ("000001.SZ", "演示保留", "保留集")], 1
        if not a.final:
            stocks = stocks[:2]
    else:
        stocks, nh = load_universe(a.final)
    if a.only:
        want = set(a.only.split(","))
        stocks = [x for x in stocks if x[0] in want]
    cnt = pd.Series([g for _, _, g in stocks]).value_counts().to_dict()
    print(f"股票 {len(stocks)} 只 {cnt}；保留集 {nh} 只{'（本次参与）' if a.final else '（本次不取数、不计算）'}")
    print(f"取 {start} → {end} 前复权日线 …\n")

    frames, fails = [], []
    for code, name, grp in stocks:
        try:
            raw = fetch_demo(code, s8, e8) if a.demo else (fetch_hk(code, s8, e8) if code.endswith(".HK")
                                                           else fetch_a(code, s8, e8))
            if raw is None or len(raw) < 300:
                raise RuntimeError(f"数据太少（{0 if raw is None else len(raw)} 根）")
            d = R.compute(raw, version=VER)
            d = evaluate(d)
            d = evaluate(d, "sup2", "res2", "buy2", "sell2")
            cut = pd.Timestamp(end - timedelta(days=round(365.25 * a.years)))
            d = d[d["date"] >= cut].copy()
            d["code"], d["name"], d["grp"] = code, name, grp
            frames.append(d)
            print(f"  ✓ [{grp}] {code} {name}  {len(raw)} 根，回测 {len(d)} 天", flush=True)
        except Exception as e:  # noqa: BLE001
            fails.append(code)
            print(f"  ✗ [{grp}] {code} {name}  {type(e).__name__}: {str(e)[:120]}", flush=True)
    if not frames:
        print("没有任何股票取到数据")
        sys.exit(1)
    all_d = pd.concat(frames, ignore_index=True)

    os.makedirs(OUT_DIR, exist_ok=True)
    lines = ["成功口径：当日乐观 = 成交当天走出 1.5% 价差；当日保守 = 成交当天收盘仍有 0.5% 以上利润；",
             "          N日内 = 成交后 N 个交易日内（不含成交当天）走出 1.5% 价差。成功率均以成交了的为分母。",
             f"规则第 {VER} 版；调参段 = 2016–2022，检验段 = 2023 以后。", ""]
    print("\n".join(lines))
    report(all_d, lines, a.final)
    if fails:
        lines.append(f"\n取数失败：{', '.join(fails)}")
        print(lines[-1])
    keep = ["grp", "code", "name", "date", "open", "high", "low", "close", "trend", "long",
            "sup", "res", "sup2", "res2", "pos", "buy_hits", "buy_n", "sell_hits", "sell_n"]
    keep += [c for c in all_d.columns if c.startswith(("buy_", "sell_", "buy2_", "sell2_")) and c not in keep]
    keep += [c for c in all_d.columns if c.startswith("c_")]
    keep += ["rsi6", "rsi24", "bias20", "bias55", "j", "vr", "atr"]
    out = all_d[[c for c in dict.fromkeys(keep) if c in all_d.columns]]
    if not a.final:
        out = out[out["grp"] != "保留集"]
    out.to_csv(os.path.join(OUT_DIR, "daily_signals.csv.gz"), index=False, encoding="utf-8-sig",
               compression="gzip")
    with open(os.path.join(OUT_DIR, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n明细已存 {OUT_DIR}/daily_signals.csv.gz，报告 {OUT_DIR}/report.txt")


if __name__ == "__main__":
    main()
