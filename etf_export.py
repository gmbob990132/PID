#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETF 份额 + 融资融券 模块 · 导出（只读 data/etf.db，写 etf_api/ 下 JSON，前端只读这些）

  etf_api/etf.json     四组 ETF（沪深300 / 上证50 / 中证500 / 科创50）：日期轴 + 每只 ETF 的总份额序列（单位：亿份，缺失为 null）
  etf_api/margin.json  融资融券：日期轴 + 全市场/沪/深 各指标序列（单位：亿元）
  etf_api/meta.json    生成时间、最新日期、最近一次运行状态
"""
import os
import sys
import json
import sqlite3
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
GROUP_NAMES = {"hs300": "沪深300 ETF", "sse50": "上证50 ETF", "csi500": "中证500 ETF", "kc50": "科创50 ETF", "cyb": "创业板 ETF"}
MARGIN_FIELDS = ["rzye", "rzmre", "rzche", "rqye", "rzrqye"]      # 融资余额/融资买入额/融资偿还额/融券余额/两融余额


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def hs300_share(conn, out_dir, gen, top_n=4):
    """规模最大的 top_n 只沪深300 ETF 的合计市值 ÷ 沪深300 流通市值（成分股流通市值之和）。
    ETF 市值 = 份额(万份) × 收盘价(元) → 万元；分母 float_mv 也是万元。只保留"4 只都有份额和收盘价、且分母有值"的日子。"""
    conn.execute("CREATE TABLE IF NOT EXISTS hs300_mv(date TEXT PRIMARY KEY, float_mv REAL NOT NULL, n_members INTEGER, n_matched INTEGER)")
    funds = conn.execute("SELECT code,name FROM etf_funds WHERE grp='hs300' ORDER BY sort").fetchall()
    sh = {c: dict(conn.execute("SELECT date,share FROM etf_share WHERE code=?", (c,)).fetchall()) for c, _ in funds}
    px = {c: dict(conn.execute("SELECT date,close FROM etf_price WHERE code=?", (c,)).fetchall()) for c, _ in funds}
    latest = []
    for c, n in funds:
        ds = sorted(set(sh[c]) & set(px[c]))
        if ds:
            latest.append((sh[c][ds[-1]] * px[c][ds[-1]], c, n))
    latest.sort(reverse=True)
    top = [(c, n) for _, c, n in latest[:top_n]]
    fm = dict(conn.execute("SELECT date,float_mv FROM hs300_mv").fetchall())
    dates = sorted(d for d in fm if top and all(d in sh[c] and d in px[c] for c, _ in top))
    parts = {c: [round(sh[c][d] * px[c][d] / fm[d] * 100, 4) for d in dates] for c, _ in top}
    tot_mv = [sum(sh[c][d] * px[c][d] for c, _ in top) for d in dates]
    obj = {"generated_at": gen, "funds": [{"code": c, "name": n} for c, n in top], "dates": dates,
           "ratio": [round(t / fm[d] * 100, 4) for t, d in zip(tot_mv, dates)], "parts": parts,
           "etf_mv": [round(t / 1e4, 1) for t in tot_mv],                 # 亿元
           "float_mv": [round(fm[d] / 1e4, 1) for d in dates]}           # 亿元
    _write(os.path.join(out_dir, "hs300_share.json"), obj)
    return len(dates)


def turnover(conn, out_dir, gen):
    conn.execute("CREATE TABLE IF NOT EXISTS turnover(date TEXT PRIMARY KEY, sh REAL, sz REAL, bj REAL, sh_close REAL)")
    rows = conn.execute("SELECT date,sh,sz,bj,sh_close FROM turnover WHERE sh IS NOT NULL AND sz IS NOT NULL ORDER BY date").fetchall()
    _write(os.path.join(out_dir, "turnover.json"), {
        "generated_at": gen, "unit": "亿元", "dates": [r[0] for r in rows],
        "sh": [r[1] for r in rows], "sz": [r[2] for r in rows], "bj": [r[3] for r in rows],
        "sh_close": [r[4] for r in rows]})
    return len(rows)


def export(db_path, out_dir):
    conn = sqlite3.connect(db_path)
    gen = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute("CREATE TABLE IF NOT EXISTS etf_price(code TEXT NOT NULL, date TEXT NOT NULL, close REAL NOT NULL, PRIMARY KEY(code, date))")

    groups = {}
    for g, gname in GROUP_NAMES.items():
        funds = conn.execute("SELECT code,name FROM etf_funds WHERE grp=? ORDER BY sort", (g,)).fetchall()
        dates = sorted({r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM etf_share WHERE code IN (SELECT code FROM etf_funds WHERE grp=?)", (g,))})
        idx = {d: i for i, d in enumerate(dates)}
        shares = {}
        for code, _ in funds:
            arr = [None] * len(dates)
            for d, s in conn.execute("SELECT date,share FROM etf_share WHERE code=?", (code,)):
                arr[idx[d]] = round(s / 1e4, 4)               # 万份 → 亿份
            shares[code] = arr
        prices = {}                                            # 收盘价（元），与 dates 对齐；用于前端算"预估买入金额"
        for code, _ in funds:
            arr = [None] * len(dates)
            for d, px in conn.execute("SELECT date,close FROM etf_price WHERE code=?", (code,)):
                if d in idx:
                    arr[idx[d]] = round(px, 4)
            prices[code] = arr
        groups[g] = {"name": gname, "funds": [{"code": c, "name": n} for c, n in funds], "dates": dates,
                     "shares": shares, "prices": prices}
    _write(os.path.join(out_dir, "etf.json"), {"generated_at": gen, "unit": "亿份", "groups": groups})

    # 融资融券：全市场 = 沪+深+北
    dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM margin ORDER BY date")]
    idx = {d: i for i, d in enumerate(dates)}
    series = {k: {f: [None] * len(dates) for f in MARGIN_FIELDS} for k in ("all", "SSE", "SZSE")}
    for r in conn.execute(f"SELECT date,exch,{','.join(MARGIN_FIELDS)} FROM margin"):
        i = idx[r[0]]
        for f, v in zip(MARGIN_FIELDS, r[2:]):
            if v is None:
                continue
            v = v / 1e8                                         # 元 → 亿元
            if r[1] in ("SSE", "SZSE"):
                series[r[1]][f][i] = round(v, 2)
            series["all"][f][i] = round((series["all"][f][i] or 0) + v, 2)
    # 沪、深有一边还没披露的日子，"全市场"只剩半边，会把合计和变化算歪（曾出现两融余额一天"腰斩"）：该日全市场置空
    for f in MARGIN_FIELDS:
        for i in range(len(dates)):
            if series["SSE"][f][i] is None or series["SZSE"][f][i] is None:
                series["all"][f][i] = None
    # 平均维持担保比例（市场整体）：单位 %。若源数据是小数（如 2.8）则 ×100
    conn.execute("CREATE TABLE IF NOT EXISTS margin_account(date TEXT PRIMARY KEY, avg_ratio REAL, guarantee REAL)")
    acc = conn.execute("SELECT date,avg_ratio,guarantee FROM margin_account ORDER BY date").fetchall()
    scale = 100.0 if acc and sorted(a[1] for a in acc)[len(acc) // 2] < 10 else 1.0
    account = {"dates": [a[0] for a in acc], "ratio": [round(a[1] * scale, 2) for a in acc],
               "guarantee": [None if a[2] is None else round(a[2] / 1e8, 1) for a in acc]}     # 担保物总价值：亿元
    _write(os.path.join(out_dir, "margin.json"), {"generated_at": gen, "unit": "亿元", "dates": dates, "series": series,
                                                  "account": account})

    runs = conn.execute("SELECT run_at,item,status,message FROM run_log ORDER BY id DESC LIMIT 40").fetchall()
    last_run = runs[0][0] if runs else None
    failed = [{"item": i, "message": m} for (t, i, s, m) in runs if s == "failed" and t == last_run]
    _write(os.path.join(out_dir, "meta.json"), {
        "generated_at": gen, "last_run": last_run, "failed": failed,
        "etf_latest": max((g["dates"][-1] for g in groups.values() if g["dates"]), default=None),
        "margin_latest": dates[-1] if dates else None})
    n_hs = hs300_share(conn, out_dir, gen)
    n_to = turnover(conn, out_dir, gen)
    conn.close()
    print(f"A股成交额：{n_to} 个交易日")
    print(f"沪深300ETF占比：{n_hs} 个交易日")
    print(f"导出完成 → {out_dir}：ETF {len(groups)} 组 {sum(len(g['funds']) for g in groups.values())} 只；融资融券 {len(dates)} 天")


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "data", "etf.db")
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "etf_api")
    if not os.path.exists(db):
        sys.exit(f"数据库不存在: {db}")
    export(db, out)
