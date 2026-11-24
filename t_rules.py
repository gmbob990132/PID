#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
做T 规则库（回测和每日计算共用）

输入：日线 DataFrame，列 date, open, high, low, close, vol（前复权；按日期升序）
输出：同一个 DataFrame 加上指标列、趋势、支撑/压力、各条件布尔列、命中数。

指标口径（同花顺/通达信习惯）：
  MA n                    收盘均线
  RSI6 / RSI24            SMA(MAX(C-LC,0),N,1) / SMA(ABS(C-LC),N,1) * 100
  KDJ(9,3,3)              RSV → K=SMA(RSV,3,1) → D=SMA(K,3,1) → J=3K-2D
  MACD(12,26,9)           柱 = 2*(DIF-DEA)
  BOLL(20,2)              中轨 MA20，上下轨 ±2 倍标准差
  BIAS20 / BIAS55         (C-MA)/MA*100；阈值用个股自身历史 5% / 95% 分位（只用当天及以前的数据）
  ATR14                   真实波幅 14 日均值
  量比                    当日量 / 20 日均量

趋势（每天独立判断）：
  上涨  MA20>MA55 且 收盘>MA55 且 MA20 高于 5 天前
  下跌  MA20<MA55 且 收盘<MA55 且 MA20 低于 5 天前
  震荡  其余
  长期  收盘在 MA233 之上 = 长期多头，之下 = 长期空头（不足 233 天为空）

支撑/压力（给"下一个交易日"用）：候选价位 = MA5/10/20/55/233、布林上下轨、当日高低点、
  20 日高低点、枢轴点 P/S1/R1、近 60 日成交密集区；当前价下方最近的为支撑，上方最近的为压力；
  1% 以内有多个候选重合的，记为"强"。位置 = (收盘-支撑)/(压力-支撑)。
"""
import numpy as np
import pandas as pd

# ---------------- 可调参数 ----------------
P = dict(
    rsi_deep=40, rsi_extreme=35, rsi6_low=25,          # 下跌正T 超卖
    bias_lo_q=0.05, bias_hi_q=0.95, bias_min_hist=250,  # BIAS 分位阈值（至少 250 天历史才启用）
    pos_low=0.33, pos_high=0.67,                        # 位置：靠近支撑 / 靠近压力
    vol_shrink=0.7, vol_surge=2.0, flat_pct=0.5,        # 缩量 / 放量 / 平盘(涨跌幅绝对值<0.5%不算方向)
    min_gap=0.003,                                      # 支撑/压力至少离收盘 0.3%
    strong_band=0.01,                                   # 1% 以内候选重合 → 强
    space_min=0.015,                                    # 支撑到压力空间 < 1.5% → 空间不足
    level_min_atr=0.5,                                  # 第二版：支撑/压力至少离收盘 0.5 个 ATR
)


def sma_cn(x, n, m=1):
    """通达信 SMA(X,N,M)"""
    return x.ewm(alpha=m / n, adjust=False).mean()


def add_indicators(df):
    d = df.copy().sort_values("date").reset_index(drop=True)
    c, h, l, o, v = d["close"], d["high"], d["low"], d["open"], d["vol"]
    for n in (5, 10, 20, 55, 233):
        d[f"ma{n}"] = c.rolling(n).mean()
    lc = c.shift(1)
    up = (c - lc).clip(lower=0)
    ab = (c - lc).abs()
    for n in (6, 24):
        d[f"rsi{n}"] = sma_cn(up, n) / sma_cn(ab, n).replace(0, np.nan) * 100
    llv, hhv = l.rolling(9).min(), h.rolling(9).max()
    rsv = (c - llv) / (hhv - llv).replace(0, np.nan) * 100
    d["k"] = sma_cn(rsv.fillna(50), 3)
    d["d"] = sma_cn(d["k"], 3)
    d["j"] = 3 * d["k"] - 2 * d["d"]
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    d["dif"] = ema12 - ema26
    d["dea"] = d["dif"].ewm(span=9, adjust=False).mean()
    d["hist"] = 2 * (d["dif"] - d["dea"])
    sd = c.rolling(20).std(ddof=0)
    d["boll_up"], d["boll_low"] = d["ma20"] + 2 * sd, d["ma20"] - 2 * sd
    for n in (20, 55):
        d[f"bias{n}"] = (c - d[f"ma{n}"]) / d[f"ma{n}"] * 100
        mh = P["bias_min_hist"]
        d[f"bias{n}_lo"] = d[f"bias{n}"].expanding(mh).quantile(P["bias_lo_q"])
        d[f"bias{n}_hi"] = d[f"bias{n}"].expanding(mh).quantile(P["bias_hi_q"])
    for q in (10, 20, 80):
        d[f"bias20_q{q}"] = d["bias20"].expanding(P["bias_min_hist"]).quantile(q / 100)
    tr = pd.concat([h - l, (h - lc).abs(), (l - lc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()
    d["vr"] = v / v.rolling(20).mean()
    d["pct"] = c.pct_change() * 100
    d["ma20_slope"] = d["ma20"] - d["ma20"].shift(5)
    d["hhv20"], d["llv20"] = h.rolling(20).max(), l.rolling(20).min()
    d["hhv60"], d["llv60"] = h.rolling(60).max(), l.rolling(60).min()
    d["rpos60"] = (c - d["llv60"]) / (d["hhv60"] - d["llv60"]).replace(0, np.nan)
    pv = (h + l + c) / 3
    d["pivot"], d["s1"], d["r1"] = pv, 2 * pv - h, 2 * pv - l
    d["poc60"] = _poc(d, 60)
    d["upper_shadow"] = (h - np.maximum(o, c)) / (h - l).replace(0, np.nan)
    return d


def _poc(d, n, bins=40):
    """近 n 日成交密集价（按 (高+低+收)/3 加权成交量的直方图峰值）"""
    out = np.full(len(d), np.nan)
    tp = ((d["high"] + d["low"] + d["close"]) / 3).to_numpy()
    vol = d["vol"].to_numpy()
    for i in range(n - 1, len(d)):
        x, w = tp[i - n + 1:i + 1], vol[i - n + 1:i + 1]
        if np.isnan(x).any() or w.sum() <= 0:
            continue
        hist, edges = np.histogram(x, bins=bins, weights=w)
        k = hist.argmax()
        out[i] = (edges[k] + edges[k + 1]) / 2
    return out


LEVEL_COLS = {"ma5": "MA5", "ma10": "MA10", "ma20": "MA20", "ma55": "MA55", "ma233": "MA233",
              "boll_up": "布林上轨", "boll_low": "布林下轨", "high": "当日高点", "low": "当日低点",
              "hhv20": "20日高点", "llv20": "20日低点", "pivot": "枢轴P", "s1": "枢轴S1", "r1": "枢轴R1",
              "poc60": "60日成交密集区"}


def levels_row(row, extra=None, version=2):
    """一行 → (支撑, 支撑来源list, 压力, 压力来源list)。extra: {名称: 价位} 额外候选（如 60/30 分钟均线）
    version=1：离收盘至少 0.3% 的最近价位；version=2：至少 max(0.3%, 0.5×ATR)"""
    c = row["close"]
    cands = [(LEVEL_COLS[k], row[k]) for k in LEVEL_COLS if pd.notna(row.get(k))]
    if extra:
        cands += [(k, v) for k, v in extra.items() if v is not None and pd.notna(v)]
    atr = row.get("atr")
    gap = c * P["min_gap"]
    if version >= 2 and pd.notna(atr):
        gap = max(gap, P["level_min_atr"] * atr)
    below = [(n, v) for n, v in cands if v < c - gap]
    above = [(n, v) for n, v in cands if v > c + gap]
    if below:
        s = max(v for _, v in below)
        s_src = [n for n, v in below if abs(v - s) / s <= P["strong_band"]]
    else:
        s, s_src = (c - atr if pd.notna(atr) else np.nan), ["收盘-ATR"]
    if above:
        r = min(v for _, v in above)
        r_src = [n for n, v in above if abs(v - r) / r <= P["strong_band"]]
    else:
        r, r_src = (c + atr if pd.notna(atr) else np.nan), ["收盘+ATR"]
    return s, s_src, r, r_src


def add_levels(d, version=2):
    out = d.apply(lambda row: pd.Series(levels_row(row, version=version),
                                        index=["sup", "sup_src", "res", "res_src"]), axis=1)
    d = pd.concat([d, out], axis=1)
    d["pos"] = (d["close"] - d["sup"]) / (d["res"] - d["sup"]).replace(0, np.nan)
    d["space"] = (d["res"] - d["sup"]) / d["close"]
    return d


def add_trend(d):
    up = (d["ma20"] > d["ma55"]) & (d["close"] > d["ma55"]) & (d["ma20_slope"] > 0)
    dn = (d["ma20"] < d["ma55"]) & (d["close"] < d["ma55"]) & (d["ma20_slope"] < 0)
    d["trend"] = np.where(up, "上涨", np.where(dn, "下跌", "震荡"))
    d.loc[d["ma55"].isna(), "trend"] = None
    d["long"] = np.where(d["ma233"].isna(), None, np.where(d["close"] > d["ma233"], "长期多头", "长期空头"))
    return d


# ---------------- 六格条件 ----------------
# 每条：(代码, 说明, 计算函数)。函数接收整张表，返回布尔 Series。
def _kx_up(d):
    return (d["k"] > d["d"]) & (d["k"].shift(1) <= d["d"].shift(1))


def _kx_dn(d):
    return (d["k"] < d["d"]) & (d["k"].shift(1) >= d["d"].shift(1))


def _down_day(d):
    return d["pct"] < -P["flat_pct"]


def _up_day(d):
    return d["pct"] > P["flat_pct"]


RULES_V1 = {
    ("下跌", "正T"): [
        ("rsi24", "RSI24<40（<35 为极度超卖）", lambda d: d["rsi24"] < P["rsi_deep"]),
        ("rsi6", "RSI6<25", lambda d: d["rsi6"] < P["rsi6_low"]),
        ("bias", "BIAS20 或 BIAS55 ≤ 自身 5% 分位",
         lambda d: (d["bias20"] <= d["bias20_lo"]) | (d["bias55"] <= d["bias55_lo"])),
        ("pos", "靠近支撑（位置<1/3）", lambda d: d["pos"] < P["pos_low"]),
        ("macd", "MACD 绿柱连续 2 天缩短",
         lambda d: (d["hist"] < 0) & (d["hist"] > d["hist"].shift(1)) & (d["hist"].shift(1) > d["hist"].shift(2))),
        ("vol", "缩量下跌（量比<0.7）", lambda d: _down_day(d) & (d["vr"] < P["vol_shrink"])),
        ("kdj", "J<0 或 KDJ 金叉", lambda d: (d["j"] < 0) | _kx_up(d)),
    ],
    ("下跌", "反T"): [
        ("pos", "靠近压力（位置>2/3）", lambda d: d["pos"] > P["pos_high"]),
        ("rsi", "RSI6>70 或 RSI24>50", lambda d: (d["rsi6"] > 70) | (d["rsi24"] > 50)),
        ("bias", "BIAS20≥0（反弹到 MA20 上方）", lambda d: d["bias20"] >= 0),
        ("macd", "MACD 红柱缩短 / 绿柱放长",
         lambda d: ((d["hist"] > 0) & (d["hist"] < d["hist"].shift(1))) |
                   ((d["hist"] < 0) & (d["hist"] < d["hist"].shift(1)))),
        ("vol", "缩量反弹（量比<0.7）", lambda d: _up_day(d) & (d["vr"] < P["vol_shrink"])),
        ("kdj", "J>100 或 KDJ 死叉", lambda d: (d["j"] > 100) | _kx_dn(d)),
    ],
    ("上涨", "正T"): [
        ("pos", "回踩 MA10/MA20 附近（1 个 ATR 内）且位置<1/2",
         lambda d: (np.minimum((d["close"] - d["ma10"]).abs(), (d["close"] - d["ma20"]).abs()) <= d["atr"])
                   & (d["pos"] < 0.5)),
        ("rsi", "RSI6<30 且 RSI24≥45", lambda d: (d["rsi6"] < 30) & (d["rsi24"] >= 45)),
        ("bias", "BIAS20 在 -3%~+1%", lambda d: d["bias20"].between(-3, 1)),
        ("vol", "缩量回调（量比<0.7）", lambda d: _down_day(d) & (d["vr"] < P["vol_shrink"])),
        ("macd", "MACD 在零轴上方", lambda d: (d["dif"] > 0) & (d["dea"] > 0)),
    ],
    ("上涨", "反T"): [
        ("bias", "BIAS20 ≥ 自身 95% 分位", lambda d: d["bias20"] >= d["bias20_hi"]),
        ("rsi", "RSI6>80", lambda d: d["rsi6"] > 80),
        ("boll", "触及布林上轨", lambda d: d["close"] >= d["boll_up"] * 0.99),
        ("vol", "放量（量比>2）+ 长上影", lambda d: (d["vr"] > P["vol_surge"]) & (d["upper_shadow"] > 0.4)),
        ("kdj", "J>100", lambda d: d["j"] > 100),
    ],
    ("震荡", "正T"): [
        ("range", "60 日区间下 1/4", lambda d: d["rpos60"] < 0.25),
        ("rsi", "RSI6<30", lambda d: d["rsi6"] < 30),
        ("kdj", "J<10", lambda d: d["j"] < 10),
    ],
    ("震荡", "反T"): [
        ("range", "60 日区间上 1/4", lambda d: d["rpos60"] > 0.75),
        ("rsi", "RSI6>70", lambda d: d["rsi6"] > 70),
        ("kdj", "J>90", lambda d: d["j"] > 90),
    ],
}


# ---- 第二版：删掉回测里无效的（缩量×3、下跌反T 的 MACD、上涨正T 的 BIAS 区间和回踩、震荡正T 的区间位置），
# ---- 补上在别的格里有效的写法（RSI6/J 极值、BIAS 自身分位、位置）
RULES_V2 = {
    ("下跌", "正T"): [
        ("rsi24", "RSI24<40（<35 为极度超卖）", lambda d: d["rsi24"] < P["rsi_deep"]),
        ("rsi6", "RSI6<25", lambda d: d["rsi6"] < P["rsi6_low"]),
        ("bias", "BIAS20 或 BIAS55 ≤ 自身 5% 分位",
         lambda d: (d["bias20"] <= d["bias20_lo"]) | (d["bias55"] <= d["bias55_lo"])),
        ("pos", "靠近支撑（位置<1/3）", lambda d: d["pos"] < P["pos_low"]),
        ("macd", "MACD 绿柱连续 2 天缩短",
         lambda d: (d["hist"] < 0) & (d["hist"] > d["hist"].shift(1)) & (d["hist"].shift(1) > d["hist"].shift(2))),
        ("kdj", "J<0 或 KDJ 金叉", lambda d: (d["j"] < 0) | _kx_up(d)),
    ],
    ("下跌", "反T"): [
        ("pos", "靠近压力（位置>2/3）", lambda d: d["pos"] > P["pos_high"]),
        ("rsi", "RSI6>70 或 RSI24>50", lambda d: (d["rsi6"] > 70) | (d["rsi24"] > 50)),
        ("bias", "BIAS20≥0（反弹到 MA20 上方）", lambda d: d["bias20"] >= 0),
        ("biasq", "BIAS20 ≥ 自身 80% 分位", lambda d: d["bias20"] >= d["bias20_q80"]),
        ("kdj", "J>90", lambda d: d["j"] > 90),
    ],
    ("上涨", "正T"): [
        ("rsi", "RSI6<30 且 RSI24≥45", lambda d: (d["rsi6"] < 30) & (d["rsi24"] >= 45)),
        ("macd", "MACD 在零轴上方", lambda d: (d["dif"] > 0) & (d["dea"] > 0)),
        ("kdj", "J<10", lambda d: d["j"] < 10),
        ("biasq", "BIAS20 ≤ 自身 20% 分位", lambda d: d["bias20"] <= d["bias20_q20"]),
        ("pos", "靠近支撑（位置<1/3）", lambda d: d["pos"] < P["pos_low"]),
    ],
    ("上涨", "反T"): RULES_V1[("上涨", "反T")],
    ("震荡", "正T"): [
        ("rsi", "RSI6<30", lambda d: d["rsi6"] < 30),
        ("kdj", "J<10", lambda d: d["j"] < 10),
        ("biasq", "BIAS20 ≤ 自身 10% 分位", lambda d: d["bias20"] <= d["bias20_q10"]),
    ],
    ("震荡", "反T"): RULES_V1[("震荡", "反T")],
}
# ---- 第三版：每条带"核心/候选"标记。命中数只数核心条件；候选条件照样计算、照样报告增益，
# ---- 等更多数据验证后再决定去留。"位置"按第一档（最近）支撑压力计算。
def _c(code, desc, fn, core=True):
    return (code, desc, fn, core)


V2 = {k: {c[0]: c for c in v} for k, v in RULES_V2.items()}
V1 = {k: {c[0]: c for c in v} for k, v in RULES_V1.items()}


def _from(src, cell, code, core=True, new_code=None):
    c = src[cell][code]
    return (new_code or c[0], c[1], c[2], core)


RULES_V3 = {
    ("下跌", "正T"): [
        _from(V2, ("下跌", "正T"), "rsi24"), _from(V2, ("下跌", "正T"), "rsi6"),
        _from(V2, ("下跌", "正T"), "bias"), _from(V2, ("下跌", "正T"), "pos"),
        _from(V2, ("下跌", "正T"), "kdj"),
        _from(V2, ("下跌", "正T"), "macd", core=False),
        _from(V1, ("下跌", "正T"), "vol", core=False),
    ],
    ("下跌", "反T"): [
        _from(V2, ("下跌", "反T"), "rsi"), _from(V2, ("下跌", "反T"), "bias"),
        _from(V2, ("下跌", "反T"), "biasq"), _from(V2, ("下跌", "反T"), "kdj", new_code="j90"),
        _from(V2, ("下跌", "反T"), "pos", core=False),
        _from(V1, ("下跌", "反T"), "macd", core=False),
        _from(V1, ("下跌", "反T"), "vol", core=False),
        _from(V1, ("下跌", "反T"), "kdj", core=False),
    ],
    ("上涨", "正T"): [
        _from(V2, ("上涨", "正T"), "macd"), _from(V2, ("上涨", "正T"), "rsi"),
        _from(V2, ("上涨", "正T"), "biasq"),
        _from(V2, ("上涨", "正T"), "kdj", core=False),
        _from(V2, ("上涨", "正T"), "pos", core=False, new_code="pos3"),
        _from(V1, ("上涨", "正T"), "pos", core=False),
        _from(V1, ("上涨", "正T"), "bias", core=False),
        _from(V1, ("上涨", "正T"), "vol", core=False),
    ],
    ("上涨", "反T"): [
        _from(V1, ("上涨", "反T"), "bias"), _from(V1, ("上涨", "反T"), "rsi"),
        _from(V1, ("上涨", "反T"), "boll"), _from(V1, ("上涨", "反T"), "kdj"),
        _from(V1, ("上涨", "反T"), "vol", core=False),
    ],
    ("震荡", "正T"): [
        _from(V2, ("震荡", "正T"), "rsi"), _from(V2, ("震荡", "正T"), "kdj"),
        _from(V2, ("震荡", "正T"), "biasq"),
        _from(V1, ("震荡", "正T"), "range", core=False),
        _c("pos", "靠近支撑（位置<1/3）", lambda d: d["pos"] < P["pos_low"], core=False),
    ],
    ("震荡", "反T"): [
        _from(V1, ("震荡", "反T"), "rsi"), _from(V1, ("震荡", "反T"), "kdj"),
        _from(V1, ("震荡", "反T"), "range"),
        _c("pos", "靠近压力（位置>2/3）", lambda d: d["pos"] > P["pos_high"], core=False),
    ],
}
RULES = RULES_V3          # 每日计算默认用第三版
RULESETS = {1: RULES_V1, 2: RULES_V2, 3: RULES_V3}


def is_core(cond):
    return cond[3] if len(cond) > 3 else True


def add_rules(d, rules=None):
    """每格条件算成布尔列 c_<趋势>_<方向>_<代码>；按当天趋势取对应格，得到 buy_hits/buy_n、sell_hits/sell_n"""
    rules = rules or RULES
    for (tr, side), conds in rules.items():
        for cond in conds:
            code, fn = cond[0], cond[2]
            d[f"c_{tr}_{side}_{code}"] = fn(d).fillna(False).astype(bool)
    d["buy_hits"] = 0
    d["sell_hits"] = 0
    d["buy_n"] = 0
    d["sell_n"] = 0
    for tr in ("上涨", "下跌", "震荡"):
        m = d["trend"] == tr
        for side, col in (("正T", "buy"), ("反T", "sell")):
            cols = [f"c_{tr}_{side}_{c[0]}" for c in rules[(tr, side)] if is_core(c)]
            d.loc[m, f"{col}_hits"] = d.loc[m, cols].sum(axis=1)
            d.loc[m, f"{col}_n"] = len(cols)
    d["buy_ratio"] = d["buy_hits"] / d["buy_n"].replace(0, np.nan)
    d["sell_ratio"] = d["sell_hits"] / d["sell_n"].replace(0, np.nan)
    return d


def compute(df, version=2, ind=None):
    """version: 1 = 第一版规则 + 最近价位；2 = 第二版规则 + 至少 0.5 ATR 的价位。
    ind: 已算好的指标表（回测时两版共用，省时间）"""
    d = (ind if ind is not None else add_trend(add_indicators(df))).copy()
    if version >= 3:
        # 第一档：最近价位（位置也按它算）；第二档：至少 0.5 个 ATR
        d = add_levels(d, version=1)
        two = d.apply(lambda row: pd.Series(levels_row(row, version=2),
                                            index=["sup2", "sup2_src", "res2", "res2_src"]), axis=1)
        d = pd.concat([d, two], axis=1)
    else:
        d = add_levels(d, version=version)
    d = add_rules(d, RULESETS[version])
    return d
