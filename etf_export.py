#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETF 份额 + 融资融券 模块 · 导出（只读 data/etf.db，写 etf_api/ 下 JSON，前端只读这些）

  etf_api/etf.json     两组 ETF（沪深300 / 中证500）：日期轴 + 每只 ETF 的总份额序列（单位：亿份，缺失为 null）
  etf_api/margin.json  融资融券：日期轴 + 全市场/沪/深 各指标序列（单位：亿元）
  etf_api/meta.json    生成时间、最新日期、最近一次运行状态
"""
import os
import sys
import json
import sqlite3
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
GROUP_NAMES = {"hs300": "沪深300 ETF", "sse50": "上证50 ETF", "csi500": "中证500 ETF"}
MARGIN_FIELDS = ["rzye", "rzmre", "rzche", "rqye", "rzrqye"]      # 融资余额/融资买入额/融资偿还额/融券余额/两融余额


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def export(db_path, out_dir):
    conn = sqlite3.connect(db_path)
    gen = datetime.now(timezone.utc).isoformat(timespec="seconds")

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
        groups[g] = {"name": gname, "funds": [{"code": c, "name": n} for c, n in funds], "dates": dates, "shares": shares}
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
    _write(os.path.join(out_dir, "margin.json"), {"generated_at": gen, "unit": "亿元", "dates": dates, "series": series})

    runs = conn.execute("SELECT run_at,item,status,message FROM run_log ORDER BY id DESC LIMIT 40").fetchall()
    last_run = runs[0][0] if runs else None
    failed = [{"item": i, "message": m} for (t, i, s, m) in runs if s == "failed" and t == last_run]
    _write(os.path.join(out_dir, "meta.json"), {
        "generated_at": gen, "last_run": last_run, "failed": failed,
        "etf_latest": max((g["dates"][-1] for g in groups.values() if g["dates"]), default=None),
        "margin_latest": dates[-1] if dates else None})
    conn.close()
    print(f"导出完成 → {out_dir}：ETF 两组 {sum(len(g['funds']) for g in groups.values())} 只；融资融券 {len(dates)} 天")


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "data", "etf.db")
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "etf_api")
    if not os.path.exists(db):
        sys.exit(f"数据库不存在: {db}")
    export(db, out)
