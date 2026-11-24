#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
做T 卖出规则回测：止盈 × 止损 × 持有期（日线 10 年）+ 当天做完（A 股 60 分钟线，最近约一年）

共同设定（和 t_pnl 一致）：每笔 10 万；A 股扣 50 元、港股扣 100 元；重叠口径 B（同股同方向同时只做一笔）；
"不看条件"档用口径 A（只看每笔均值，作参照）。
  止盈：+1.5% / +2% / +3% / 1 倍 ATR（按信号日 ATR/收盘价）
  止损：不设 / -2% / -3%
  持有：1 / 2 / 3 天（买入当天不算）

日线规则（每天按 开盘 → 止损 → 止盈 的顺序判断；同一天两者都碰到，按先止损，偏保守）：
  正T：当天最低 ≤ 止损价 → 止损（开盘就低于止损价按开盘价）；否则最高 ≥ 止盈价 → 止盈（开盘就高于止盈价按开盘价）
  反T：方向相反。到期都没触发 → 第 N 天收盘结算。

当天做完（60 分钟线；新浪不复权，用 Tushare 前复权日线换算成同一口径）：
  成交日逐根 K 线：第一根最低价 ≤ 支撑的 K 线成交（开盘就低于支撑按开盘价）；
  之后的 K 线（不含成交那根）按 止损 → 止盈 判断；
  当天没触发：
    "当天收盘"   → 收盘卖出（反T 收盘买回）
    "延续 N 天"  → 接着按上面的日线规则再走 N 天
"""
import json
import time
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

import t_pnl

NOTIONAL = t_pnl.NOTIONAL
TARGETS = [("1.5%", 0.015), ("2%", 0.02), ("3%", 0.03), ("1ATR", "atr")]
STOPS = [("不设", None), ("-2%", 0.02), ("-3%", 0.03)]
HORIZONS = (1, 2, 3)
COMBOS = [(tl, tv, sl, sv, n) for tl, tv in TARGETS for sl, sv in STOPS for n in HORIZONS]
TIERS_B = ("强", "一般")
TREND_ID = {"下跌": 0, "上涨": 1, "震荡": 2}
TREND_NAME = {v: k for k, v in TREND_ID.items()}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def combo_name(c):
    tl, _, sl, _, n = c
    return f"止盈{tl} 止损{sl} {n}天"


# ---------------- 日线：按组合计算每个潜在交易的结果 ----------------
def entry(d, side):
    o1 = d["open"].shift(-1)
    if side == "正T":
        lvl = d["sup"]
        fill = (d["low"].shift(-1) <= lvl) & o1.notna()
        px = np.where(o1 < lvl, o1, lvl)
    else:
        lvl = d["res"]
        fill = (d["high"].shift(-1) >= lvl) & o1.notna()
        px = np.where(o1 > lvl, o1, lvl)
    return fill.to_numpy(), np.asarray(px, dtype=float)


def daily_outcome(O, H, L, C, start, px, side, tgt, stop, n):
    """向量化：start = 第一根要判断的日线下标（数组），px 成交价，tgt 止盈幅度（数组），stop 止损幅度或 None。
    返回 (有效, 退出下标, 毛收益率, 止盈, 止损)"""
    m = len(O)
    k = start.size
    alive = np.ones(k, bool)
    out_px = np.full(k, np.nan)
    exit_i = np.full(k, -1)
    hit = np.zeros(k, bool)
    stp = np.zeros(k, bool)
    last = start + n - 1
    valid = last < m
    for j in range(n):
        idx = start + j
        ok = alive & (idx < m)
        ii = np.where(ok, idx, 0)
        o, h, l = O[ii], H[ii], L[ii]
        if side == "正T":
            if stop is not None:
                sp = px * (1 - stop)
                s_hit = ok & (l <= sp)
                out_px = np.where(s_hit, np.minimum(o, sp), out_px)
            else:
                s_hit = np.zeros(k, bool)
            tp = px * (1 + tgt)
            t_hit = ok & ~s_hit & (h >= tp)
            out_px = np.where(t_hit, np.maximum(o, tp), out_px)
        else:
            if stop is not None:
                sp = px * (1 + stop)
                s_hit = ok & (h >= sp)
                out_px = np.where(s_hit, np.maximum(o, sp), out_px)
            else:
                s_hit = np.zeros(k, bool)
            tp = px * (1 - tgt)
            t_hit = ok & ~s_hit & (l <= tp)
            out_px = np.where(t_hit, np.minimum(o, tp), out_px)
        done = s_hit | t_hit
        exit_i = np.where(done, idx, exit_i)
        hit |= t_hit
        stp |= s_hit
        alive &= ~done
    li = np.where(valid, last, 0)
    out_px = np.where(alive, C[li], out_px)
    exit_i = np.where(alive, last, exit_i)
    gross = (out_px / px - 1) if side == "正T" else (1 - out_px / px)
    return valid, exit_i, gross, hit, stp


def keep_b(e, x):
    keep = np.zeros(e.size, bool)
    last = -1
    el, xl = e.tolist(), x.tolist()
    for k in range(len(el)):
        if el[k] > last:
            keep[k] = True
            last = xl[k]
    return keep


class Acc:
    """累加器：key → [笔数, 盈亏和, 止盈数, 止损数, 盈利笔数]"""

    def __init__(self):
        self.v = {}

    def add(self, key, pnl, hit, stp):
        a = self.v.setdefault(key, np.zeros(5))
        a += [pnl.size, pnl.sum(), hit.sum(), stp.sum(), (pnl > 0).sum()]

    def frame(self, cols):
        rows = [list(k) + list(v) for k, v in self.v.items()]
        return pd.DataFrame(rows, columns=cols + ["n", "pnl_sum", "hits", "stops", "wins"])


def seg_codes(code, grp, dates, split, min_cov):
    """每个信号日属于哪些段：返回 {段名: 布尔数组}"""
    segs = {}
    if grp == "保留集":
        segs["保留集"] = np.ones(len(dates), bool)
    else:
        segs["调参段"] = dates < split
        segs["检验段·" + grp] = dates >= split
    cov = min_cov.get(code)
    if cov is not None:
        a, b = cov
        segs["分钟期·" + grp] = (dates >= a) & (dates <= b)
    return segs


def run_daily_grid(frames, rules, split, min_cov):
    acc = Acc()
    years = {}
    for code, grp, d in frames:
        d = d.reset_index(drop=True)
        cost = t_pnl.COST_HK if code.endswith(".HK") else t_pnl.COST_A
        dates = d["date"].to_numpy()
        segs = seg_codes(code, grp, dates, split, min_cov)
        for sname, m in segs.items():
            if m.any():
                yd = (d["date"][m].max() - d["date"][m].min()).days / 365.25
                years[sname] = years.get(sname, 0) + yd
        O, H, L, C = (d[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close"))
        atrp = (d["atr"] / d["close"]).to_numpy()
        tr = d["trend"].map(TREND_ID).fillna(-1).to_numpy().astype(int)
        for side in ("正T", "反T"):
            fill, px = entry(d, side)
            masks = t_pnl.tier_masks(d, side, rules)
            for c in COMBOS:
                tl, tv, sl, sv, n = c
                tgt = atrp if tv == "atr" else np.full(len(d), tv)
                for tier in ("强", "一般", "不看条件"):
                    idx = np.flatnonzero(masks[tier].to_numpy() & fill & np.isfinite(tgt))
                    if idx.size == 0:
                        continue
                    e = idx + 1
                    valid, xi, gross, hit, stp = daily_outcome(O, H, L, C, e + 1, px[idx], side, tgt[idx], sv, n)
                    idx, e, xi, gross, hit, stp = idx[valid], e[valid], xi[valid], gross[valid], hit[valid], stp[valid]
                    if idx.size == 0:
                        continue
                    kb = keep_b(e, xi) if tier != "不看条件" else np.ones(idx.size, bool)
                    pnl = NOTIONAL * gross - cost
                    for sname, m in segs.items():
                        sel = kb & m[idx]
                        if not sel.any():
                            continue
                        for t in (0, 1, 2):
                            s2 = sel & (tr[idx] == t)
                            if s2.any():
                                acc.add((sname, side, tier, combo_name(c), t), pnl[s2], hit[s2], stp[s2])
    return acc.frame(["seg", "side", "tier", "combo", "trend"]), years


# ---------------- 60 分钟线 ----------------
def fetch_min_sina(code, scale=60):
    num, mkt = code.split(".")
    sym = ("sh" if mkt == "SH" else ("bj" if mkt == "BJ" else "sz")) + num
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sym}&scale={scale}&ma=no&datalen=1023")
    last = None
    for attempt in range(4):
        try:
            with urlopen(Request(url, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"}),
                         timeout=20) as r:
                txt = r.read().decode("utf-8", "replace")
            arr = json.loads(txt) if txt.strip().startswith("[") else []
            if not arr:
                raise RuntimeError("返回空")
            df = pd.DataFrame(arr)
            df["dt"] = pd.to_datetime(df["day"])
            for c in ("open", "high", "low", "close"):
                df[c] = df[c].astype(float)
            df["date"] = df["dt"].dt.normalize()
            return df[["dt", "date", "open", "high", "low", "close"]].sort_values("dt").reset_index(drop=True)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"新浪 60 分钟线失败：{type(last).__name__}: {str(last)[:100]}")


def to_qfq(mins, daily):
    """不复权分钟线 → 前复权：每天的比例 = 前复权日线收盘 / 当天最后一根分钟线收盘"""
    raw_close = mins.groupby("date")["close"].last()
    qfq = daily.set_index("date")["close"]
    ratio = (qfq.reindex(raw_close.index) / raw_close).dropna()
    m = mins[mins["date"].isin(ratio.index)].copy()
    r = m["date"].map(ratio)
    for c in ("open", "high", "low", "close"):
        m[c] = m[c] * r
    return m


def demo_minutes(daily):
    """离线自检：把日线拆成 4 根 60 分钟线（开→低/高→收）"""
    rows = []
    for _, r in daily.tail(250).iterrows():
        o, h, l, c = r["open"], r["high"], r["low"], r["close"]
        mid1, mid2 = (l, h) if (r["date"].day % 2) else (h, l)
        pts = [o, mid1, mid2, c]
        for k in range(4):
            a, b = pts[k], pts[min(k + 1, 3)]
            rows.append({"dt": r["date"] + pd.Timedelta(hours=10 + k), "date": r["date"],
                         "open": a, "high": max(a, b), "low": min(a, b), "close": b})
    return pd.DataFrame(rows)


def run_intraday(frames, mins_map, rules):
    """返回 (累加结果 DataFrame, 每段股票×年)"""
    acc = Acc()
    years = {}
    variants = [("当天收盘", 0)] + [(f"延续{n}天", n) for n in HORIZONS]
    for code, grp, d in frames:
        mins = mins_map.get(code)
        if mins is None or mins.empty:
            continue
        d = d.reset_index(drop=True)
        cost = t_pnl.COST_HK if code.endswith(".HK") else t_pnl.COST_A
        seg = "分钟期·" + grp
        cov_dates = set(mins["date"].unique())
        dts = d["date"]
        years[seg] = years.get(seg, 0) + (mins["date"].max() - mins["date"].min()).days / 365.25
        by_day = {k: g for k, g in mins.groupby("date")}
        O, H, L, C = (d[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close"))
        atrp = (d["atr"] / d["close"]).to_numpy()
        tr = d["trend"].map(TREND_ID).fillna(-1).to_numpy().astype(int)
        for side in ("正T", "反T"):
            masks = t_pnl.tier_masks(d, side, rules)
            lvl = (d["sup"] if side == "正T" else d["res"]).to_numpy()
            cand = np.flatnonzero(masks["不看条件"].to_numpy())
            trades = []      # (i, e, px, bars_after(list of (o,h,l)), day_close)
            for i in cand:
                e = i + 1
                if e >= len(d) or dts[e] not in cov_dates:
                    continue
                g = by_day[dts[e]]
                bo, bh, bl, bc = (g[c].to_numpy() for c in ("open", "high", "low", "close"))
                S = lvl[i]
                hitbar = np.flatnonzero(bl <= S) if side == "正T" else np.flatnonzero(bh >= S)
                if hitbar.size == 0:
                    continue
                b0 = hitbar[0]
                px = (bo[b0] if bo[b0] < S else S) if side == "正T" else (bo[b0] if bo[b0] > S else S)
                after = list(zip(bo[b0 + 1:], bh[b0 + 1:], bl[b0 + 1:]))
                trades.append((i, e, px, after, bc[-1]))
            if not trades:
                continue
            tier_of = {t: masks[t].to_numpy() for t in ("强", "一般", "不看条件")}
            for tl, tv, sl, sv in [(a, b, c_, d_) for a, b in TARGETS for c_, d_ in STOPS]:
                for vname, n in variants:
                    res = []     # (i, e, x, pnl, hit, stp)
                    for i, e, px, after, dclose in trades:
                        t = atrp[i] if tv == "atr" else tv
                        if not np.isfinite(t):
                            continue
                        out, hit, stp = None, False, False
                        for o, h, l in after:
                            if side == "正T":
                                if sv is not None and l <= px * (1 - sv):
                                    out, stp = min(o, px * (1 - sv)), True
                                    break
                                if h >= px * (1 + t):
                                    out, hit = max(o, px * (1 + t)), True
                                    break
                            else:
                                if sv is not None and h >= px * (1 + sv):
                                    out, stp = max(o, px * (1 + sv)), True
                                    break
                                if l <= px * (1 - t):
                                    out, hit = min(o, px * (1 - t)), True
                                    break
                        x = e
                        if out is None:
                            if n == 0:
                                out = dclose
                            else:
                                if e + n >= len(d):
                                    continue
                                v, xi, gr, h2, s2 = daily_outcome(O, H, L, C, np.array([e + 1]), np.array([px]),
                                                                  side, np.array([t]), sv, n)
                                if not v[0]:
                                    continue
                                g0 = float(gr[0])
                                out = px * (1 + g0) if side == "正T" else px * (1 - g0)
                                hit, stp, x = bool(h2[0]), bool(s2[0]), int(xi[0])
                        gross = (out / px - 1) if side == "正T" else (1 - out / px)
                        res.append((i, e, x, NOTIONAL * gross - cost, hit, stp))
                    if not res:
                        continue
                    arr = np.array(res, dtype=float)
                    ii = arr[:, 0].astype(int)
                    for tier in ("强", "一般", "不看条件"):
                        sel = tier_of[tier][ii]
                        if not sel.any():
                            continue
                        sub = arr[sel]
                        kb = keep_b(sub[:, 1].astype(int), sub[:, 2].astype(int)) if tier != "不看条件" \
                            else np.ones(len(sub), bool)
                        sub = sub[kb]
                        trs = tr[sub[:, 0].astype(int)]
                        for tcode in (0, 1, 2):
                            s3 = sub[trs == tcode]
                            if len(s3):
                                acc.add((seg, side, tier, f"止盈{tl} 止损{sl} {vname}", tcode),
                                        s3[:, 3], s3[:, 4].astype(bool), s3[:, 5].astype(bool))
    return acc.frame(["seg", "side", "tier", "combo", "trend"]), years


# ---------------- 报告 ----------------
def _agg(df, by):
    g = df.groupby(by, as_index=False)[["n", "pnl_sum", "hits", "stops", "wins"]].sum()
    g["avg"] = g["pnl_sum"] / g["n"]
    return g


def _cell(g, years, seg):
    if g is None or g.empty or g["n"].iloc[0] == 0:
        return "—"
    r = g.iloc[0]
    yrs = years.get(seg, np.nan)
    fy = r["n"] / yrs if yrs and yrs > 0 else np.nan
    return (f"{r['avg']:>6,.0f}元 胜{r['wins'] / r['n'] * 100:4.1f}% 盈{r['hits'] / r['n'] * 100:4.1f}%"
            f" 损{r['stops'] / r['n'] * 100:4.1f}% 年{fy:4.1f}次/{r['avg'] * fy:>7,.0f}元")


def exit_report(grid, years, p, has_final=False):
    p("\n## 7. 卖出规则对比（日线；止盈 × 止损 × 持有期）")
    p("  每格：每笔平均盈亏 / 胜率（扣费后盈利）/ 止盈触发率 / 止损触发率 / 每股每年次数与收益")
    p("  排序只看调参段每笔平均；检验段只用来确认。'原规则' = 止盈1.5% 不设止损 3天。")
    allt = _agg(grid, ["seg", "side", "tier", "combo"])
    segs = ["调参段", "检验段·重点", "检验段·非重点"] + (["保留集"] if has_final else [])
    for side in ("正T", "反T"):
        for tier in ("强", "一般", "不看条件"):
            tr_ = allt[(allt["seg"] == "调参段") & (allt["side"] == side) & (allt["tier"] == tier)]
            if tr_.empty:
                continue
            top = tr_.sort_values("avg", ascending=False)["combo"].tolist()[:5]
            orig = "止盈1.5% 止损不设 3天"
            show = top + ([orig] if orig not in top else [])
            p(f"\n  ### {side} · {tier}{'（口径A，仅作参照）' if tier == '不看条件' else ''}")
            for cname in show:
                tag = "（原规则）" if cname == orig else ""
                p(f"  {cname}{tag}")
                for s in segs:
                    g = allt[(allt["seg"] == s) & (allt["side"] == side) & (allt["tier"] == tier)
                             & (allt["combo"] == cname)]
                    p(f"     {s:<10} {_cell(g, years, s)}")
    # 分格：每个 方向×档位 的调参段第一名组合
    p("\n  ### 分格明细：各 方向×档位 调参段第一名组合，在六格里的每笔平均（调参段 / 检验段·重点 / 检验段·非重点）")
    bycell = _agg(grid, ["seg", "side", "tier", "combo", "trend"])
    for side in ("正T", "反T"):
        for tier in TIERS_B:
            tr_ = allt[(allt["seg"] == "调参段") & (allt["side"] == side) & (allt["tier"] == tier)]
            if tr_.empty:
                continue
            best = tr_.sort_values("avg", ascending=False)["combo"].iloc[0]
            p(f"  {side}·{tier}  最佳：{best}")
            for t in (0, 1, 2):
                vals = []
                for s in segs[:3]:
                    g = bycell[(bycell["seg"] == s) & (bycell["side"] == side) & (bycell["tier"] == tier)
                               & (bycell["combo"] == best) & (bycell["trend"] == t)]
                    vals.append("—" if g.empty else f"{g['avg'].iloc[0]:>6,.0f}元({int(g['n'].iloc[0])}笔)")
                p(f"     {TREND_NAME[t]}{side}  " + "  /  ".join(vals))


def intraday_report(ig, iyears, grid, years, p):
    p("\n## 8. 当天做完（A 股 60 分钟线，最近约一年；只作评估，不调参）")
    p("  对照列 = 同一时期、同一档位，用日线规则（买入当天不算）的同样止盈止损、持有 3 天")
    if ig is None or ig.empty:
        p("  没有分钟线数据")
        return
    allt = _agg(ig, ["seg", "side", "tier", "combo"])
    dg = _agg(grid, ["seg", "side", "tier", "combo"])
    for seg in sorted(allt["seg"].unique()):
        for side in ("正T", "反T"):
            for tier in ("强", "一般", "不看条件"):
                s = allt[(allt["seg"] == seg) & (allt["side"] == side) & (allt["tier"] == tier)]
                if s.empty:
                    continue
                p(f"\n  ### {seg} · {side} · {tier}（股票×年 {iyears.get(seg, 0):.0f}）")
                for _, r in s.sort_values("avg", ascending=False).head(6).iterrows():
                    base = r["combo"].rsplit(" ", 1)[0]
                    dd = dg[(dg["seg"] == seg) & (dg["side"] == side) & (dg["tier"] == tier)
                            & (dg["combo"] == base + " 3天")]
                    ref = "—" if dd.empty else f"{dd['avg'].iloc[0]:,.0f}元"
                    g = s[s["combo"] == r["combo"]]
                    p(f"  {r['combo']:<26} {_cell(g, iyears, seg)}   │ 日线对照 {ref}")
                for vname in ("当天收盘",):
                    g = s[s["combo"] == f"止盈1.5% 止损不设 {vname}"]
                    if not g.empty:
                        p(f"  {'（参照）止盈1.5% 止损不设 当天收盘':<26} {_cell(g, iyears, seg)}")
