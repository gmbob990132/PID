#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
做T 模拟收益 + 资金占用（由 t_backtest.py 调用）

每笔 10 万元（港股也按 10 万人民币算，不换汇）；成本：A 股一买一卖合计万分之五（50 元），港股千分之一（100 元）。
正T：信号次日跌到支撑买入（开盘低于支撑按开盘价）；之后第 1..N 天（买入当天不算）只要最高价 ≥ 买价×1.015，
     就在 +1.5% 卖出底仓；N 天都没到，第 N 天收盘卖出。
反T：信号次日涨到压力卖出底仓（开盘高于压力按开盘价）；之后第 1..N 天只要最低价 ≤ 卖价×0.985 就买回；
     否则第 N 天收盘买回。
N = 1 / 2 / 3。
档位：强 = 核心命中 ≥ 门槛；一般 = ≥ 门槛-1；宽 = ≥1 条；不看条件 = 每天都挂单。
重叠：B = 同一只股票同一方向同一时间只做一笔（前一笔结束当天及之前出现的新成交忽略）；A = 每个信号都算。
资金占用：每个交易日收盘时，仍未结束的T仓位数 × 10 万（正T = 占用现金；反T = 需要的底仓）。
"""
import math

import numpy as np
import pandas as pd

import t_rules as R

NOTIONAL = 100_000
TARGET = 0.015
COST_A, COST_HK = 50.0, 100.0
HORIZONS = (1, 2, 3)
TIERS = ("强", "一般", "宽(≥1)", "不看条件")
QS = [("最小", 0.0), ("10%", 0.1), ("20%", 0.2), ("中位", 0.5), ("80%", 0.8), ("90%", 0.9), ("最大", 1.0)]


def _thresholds(rules):
    out = {}
    for (tr, side), conds in rules.items():
        n = sum(1 for c in conds if R.is_core(c))
        k = max(1, math.ceil(0.6 * n))
        out[(tr, side)] = k
    return out


def tier_masks(d, side, rules):
    """返回 {档位: 布尔 Series}，side = 正T / 反T"""
    col = "buy_hits" if side == "正T" else "sell_hits"
    ks = _thresholds(rules)
    k = d["trend"].map(lambda t: ks.get((t, side)) if t in ("上涨", "下跌", "震荡") else np.nan)
    base = d["trend"].isin(["上涨", "下跌", "震荡"]) & d["sup"].notna() & d["res"].notna()
    h = d[col]
    return {"强": base & (h >= k), "一般": base & (h >= (k - 1).clip(lower=1)),
            "宽(≥1)": base & (h >= 1), "不看条件": base}


def trade_outcomes(d, side, n):
    """每个信号日 i 的潜在交易：是否成交、成交日 e=i+1、退出偏移（相对 e）、毛收益率、是否达标"""
    o1 = d["open"].shift(-1)
    if side == "正T":
        lvl = d["sup"]
        fill = (d["low"].shift(-1) <= lvl) & o1.notna()
        px = np.where(o1 < lvl, o1, lvl)
    else:
        lvl = d["res"]
        fill = (d["high"].shift(-1) >= lvl) & o1.notna()
        px = np.where(o1 > lvl, o1, lvl)
    px = pd.Series(px, index=d.index)
    hit_off = pd.Series(np.nan, index=d.index)
    for j in range(n, 0, -1):                        # 从后往前覆盖 → 留下最早达标的 j
        if side == "正T":
            hj = d["high"].shift(-(1 + j)) >= px * (1 + TARGET)
        else:
            hj = d["low"].shift(-(1 + j)) <= px * (1 - TARGET)
        hit_off = hit_off.mask(hj.fillna(False), j)
    close_n = d["close"].shift(-(1 + n))
    ok = fill & close_n.notna()
    hit = hit_off.notna()
    exit_off = hit_off.fillna(n)
    if side == "正T":
        gross = np.where(hit, TARGET, close_n / px - 1)
    else:
        gross = np.where(hit, TARGET, 1 - close_n / px)
    return pd.DataFrame({"ok": ok, "hit": hit & ok, "exit_off": exit_off, "gross": gross})


def simulate(frames, rules):
    """frames: [(code, grp, d)]；返回 trades DataFrame（含 A/B 两种重叠口径标记）"""
    rows = []
    for code, grp, d in frames:
        d = d.reset_index(drop=True)
        cost = COST_HK if code.endswith(".HK") else COST_A
        dates = d["date"].to_numpy()
        for side in ("正T", "反T"):
            masks = tier_masks(d, side, rules)
            for n in HORIZONS:
                out = trade_outcomes(d, side, n)
                ok = out["ok"].to_numpy()
                ex = out["exit_off"].to_numpy().astype(int)
                hit = out["hit"].to_numpy()
                pnl = NOTIONAL * out["gross"].to_numpy() - cost
                for tier, m in masks.items():
                    idx = np.flatnonzero(m.to_numpy() & ok)
                    if idx.size == 0:
                        continue
                    e = idx + 1
                    x = e + ex[idx]
                    keep = np.zeros(idx.size, dtype=bool)
                    last = -1
                    for k in range(idx.size):          # B 口径：前一笔结束前的新成交忽略
                        if e[k] > last:
                            keep[k] = True
                            last = x[k]
                    rows.append(pd.DataFrame({
                        "code": code, "grp": grp, "side": side, "n": n, "tier": tier,
                        "sig_date": dates[idx], "e": e, "x": x, "hit": hit[idx], "pnl": pnl[idx], "keep_b": keep,
                        "open_from": dates[e], "open_to": dates[np.maximum(x - 1, e)]}))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def _years(frames, pick):
    tot = 0.0
    for code, grp, d in frames:
        s = d[pick(d, grp)]
        if len(s):
            tot += (s["date"].max() - s["date"].min()).days / 365.25
    return max(tot, 1e-9)


def money(x):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:,.0f}"


def pnl_report(trades, frames, segs, p):
    """segs: [(名称, trades 过滤函数, frames 过滤函数)]"""
    p("\n## 5. 模拟收益（每笔 10 万；A 股扣 50 元、港股扣 100 元；重叠按 B：同股同方向同时只做一笔）")
    p("  达标 = N 天内走出 1.5%；未达标均 = 没达标、到期收盘被迫结算的那些笔的平均盈亏；"
      "胜率 = 扣费后盈利的笔数占比")
    for name, tf, ff in segs:
        yrs = _years(frames, ff)
        t = trades[tf(trades) & trades["keep_b"]]
        for side in ("正T", "反T"):
            p(f"\n  ### {name} · {side}（股票×年 = {yrs:.0f}）")
            p(f"  {'档位':<8}{'持有':>4}{'笔数':>8}{'达标率':>8}{'未达标均(元)':>12}{'每笔均(元)':>11}"
              f"{'胜率':>7}{'每股每年次':>10}{'每股每年收益(元)':>15}")
            for tier in TIERS:
                for n in HORIZONS:
                    g = t[(t["side"] == side) & (t["tier"] == tier) & (t["n"] == n)]
                    if g.empty:
                        continue
                    miss = g.loc[~g["hit"], "pnl"]
                    freq = len(g) / yrs
                    avg = g["pnl"].mean()
                    p(f"  {tier:<8}{n:>3}天{len(g):>8}{g['hit'].mean() * 100:>7.1f}%"
                      f"{money(miss.mean() if len(miss) else np.nan):>12}{money(avg):>11}"
                      f"{(g['pnl'] > 0).mean() * 100:>6.1f}%{freq:>10.1f}{money(avg * freq):>15}")
    # A 口径对照（只给调参段）
    name, tf, ff = segs[0]
    p(f"\n  ### 对照：重叠口径 A（每个信号都算）vs B，{name}，持有 3 天")
    p(f"  {'方向':<4}{'档位':<8}{'A 笔数':>8}{'A 胜率':>8}{'A 每笔均':>10}{'B 笔数':>8}{'B 胜率':>8}{'B 每笔均':>10}")
    t = trades[tf(trades) & (trades["n"] == 3)]
    for side in ("正T", "反T"):
        for tier in TIERS:
            g = t[(t["side"] == side) & (t["tier"] == tier)]
            if g.empty:
                continue
            gb = g[g["keep_b"]]
            p(f"  {side:<5}{tier:<8}{len(g):>8}{(g['pnl'] > 0).mean() * 100:>7.1f}%{money(g['pnl'].mean()):>10}"
              f"{len(gb):>8}{(gb['pnl'] > 0).mean() * 100:>7.1f}%{money(gb['pnl'].mean()):>10}")
    # 分格明细（强/一般 × 1/2/3 天，B 口径）
    p("\n  ### 分格明细（B 口径；每笔均 / 每股每年收益，单位元）")
    for name, tf, ff in segs:
        yrs = _years(frames, ff)
        t = trades[tf(trades) & trades["keep_b"] & trades["tier"].isin(["强", "一般"])]
        if t.empty:
            continue
        trend_of = {}
        for code, grp, d in frames:
            trend_of[code] = dict(zip(d["date"], d["trend"]))
        t = t.assign(trend=[trend_of.get(c, {}).get(dt) for c, dt in zip(t["code"], t["sig_date"])])
        p(f"\n  {name}")
        p(f"  {'格':<10}{'档位':<6}" + "".join(f"{f'{n}天 每笔/每年':>20}" for n in HORIZONS))
        for tr in ("下跌", "上涨", "震荡"):
            for side in ("正T", "反T"):
                for tier in ("强", "一般"):
                    line = f"  {tr + '·' + side:<10}{tier:<6}"
                    for n in HORIZONS:
                        g = t[(t["trend"] == tr) & (t["side"] == side) & (t["tier"] == tier) & (t["n"] == n)]
                        if g.empty:
                            line += f"{'—':>20}"
                            continue
                        avg = g["pnl"].mean()
                        line += f"{money(avg):>9} / {money(avg * len(g) / yrs):>8}"
                    p(line)


def capital_report(trades, frames, p, grp="重点"):
    p(f"\n## 6. 资金占用：{grp}股全做（全部年份，B 口径），每个交易日收盘时仍未结束的T仓位合计")
    p("  正T = 额外买入占用的现金；反T = 先卖出所需的底仓。单位：万元。另列每天新成交笔数。")
    cal = sorted(set().union(*[set(d["date"]) for code, g, d in frames if g == grp]))
    if not cal:
        return
    cal = pd.DatetimeIndex(cal)
    t = trades[(trades["grp"] == grp) & trades["keep_b"]]
    hdr = "".join(f"{q[0]:>8}" for q in QS[:4]) + f"{'平均':>8}" + "".join(f"{q[0]:>8}" for q in QS[4:])
    for side in ("正T", "反T"):
        p(f"\n  ### {side}")
        p(f"  {'档位':<8}{'持有':>4} {'指标':<10}" + hdr)
        for tier in TIERS:
            for n in HORIZONS:
                g = t[(t["side"] == side) & (t["tier"] == tier) & (t["n"] == n)]
                if g.empty:
                    continue
                pos = pd.Series(np.arange(len(cal)), index=cal)
                diff = np.zeros(len(cal) + 1)
                np.add.at(diff, pos.loc[g["open_from"]].to_numpy(), NOTIONAL / 10_000)
                np.add.at(diff, pos.loc[g["open_to"]].to_numpy() + 1, -NOTIONAL / 10_000)
                occ = pd.Series(np.cumsum(diff[:-1]), index=cal)
                new = g.groupby("open_from").size().reindex(cal, fill_value=0)
                for lab, s in (("占用(万元)", occ), ("新成交(笔)", new)):
                    vals = [s.quantile(q) for _, q in QS[:4]] + [s.mean()] + [s.quantile(q) for _, q in QS[4:]]
                    p(f"  {tier:<8}{n:>3}天 {lab:<10}" + "".join(f"{v:>8.1f}" for v in vals))
