#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技术指标分析 · MACD 模块 · 导出脚本（只读 tech.db，写 tech_api/ 下 JSON）

产出（前端只读这些，不碰数据库）：
  tech_api/macd/meta.json            元信息：最新日期、可选日期清单、状态环、默认阈值、最近一次运行状态
  tech_api/macd/latest.json          最新交易日的完整快照（页面打开即读，一个文件搞定）
  tech_api/macd/days/YYYYMM.json     按月分片的历史快照（用户选历史日期时按需读取）
  tech_api/stocks.json               股票池（含"待计算"的新股、已停用的股票），供"股票池"页使用

快照口径（某个日期 D）：
  对每只启用的股票取"≤D 的最近一根 K 线"，所以 A 股休市而港股开市的日子，A 股显示上一个交易日的状态
  （该行 bar 字段会写明真实 K 线日期）。"当日切换"只对 K 线日期==D 的行有效。

行格式（列式字段名见 fields，省体积）：
  code, close, pct, diff, dea, hist, state, since(入此状态日期), days(已持续交易日),
  prev(今日切换前状态，无切换为""), trig(触发事件), lcx(最近一次日线交叉 G金叉/D死叉), lcd(其日期), bar(过期时的K线日期)
"""
import os
import sys
import json
import sqlite3
import argparse
from bisect import bisect_right
from datetime import datetime, timezone, timedelta

SCHEMA_VERSION = 1
CN_TZ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_RING = ["弱", "中性偏强", "极强", "强", "中性偏弱", "极弱"]
DEFAULT_RATIO = 0.005   # 零轴附近阈值 |DIFF|/收盘价 < 0.5%
FIELDS = ["code", "close", "pct", "diff", "dea", "hist", "state", "since", "days",
          "prev", "trig", "lcx", "lcd", "bar"]


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def export(db_path, out_dir):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    gen = datetime.now(timezone.utc).isoformat(timespec="seconds")

    stocks = conn.execute("SELECT code,name,market,focus,enabled,added_at,removed_at FROM stocks ORDER BY market DESC,code").fetchall()
    enabled = [s["code"] for s in stocks if s["enabled"]]
    has_data = {r[0] for r in conn.execute("SELECT DISTINCT code FROM macd_daily")}

    # ---- 每只股票的日线序列 + 状态连续段 ----
    series = {}
    for code in enabled:
        rows = conn.execute("SELECT date,close,pct_chg,diff,dea,hist,state,prev_state,trigger FROM macd_daily "
                            "WHERE code=? ORDER BY date", (code,)).fetchall()
        if rows:
            series[code] = rows
    run_start = {}   # code -> 与 rows 等长的"本状态起始 index"
    for code, rows in series.items():
        starts, s = [], 0
        for i, r in enumerate(rows):
            if i == 0 or r["state"] != rows[i - 1]["state"]:
                s = i
            starts.append(s)
        run_start[code] = starts

    # ---- 日线交叉（供"最近一次金叉/死叉"）与全部交叉（供当日信号区）----
    cross_all = {}
    for r in conn.execute("SELECT code,period,kline_date,kind,close,diff,dea,ratio FROM macd_cross ORDER BY kline_date"):
        if r["code"] in series:
            cross_all.setdefault(r["code"], []).append(r)
    daily_cross = {c: [x for x in v if x["period"] == "D"] for c, v in cross_all.items()}
    daily_cross_dates = {c: [x["kline_date"] for x in v] for c, v in daily_cross.items()}
    crosses_by_date = {}
    for c, v in cross_all.items():
        for x in v:
            crosses_by_date.setdefault(x["kline_date"], []).append(
                [c, x["period"], "G" if x["kind"] == "golden" else "D", x["close"], x["diff"], x["dea"], x["ratio"]])

    # ---- 逐日构建快照（带向前填充）----
    all_dates = sorted({r["date"] for rows in series.values() for r in rows})
    ptr = {c: -1 for c in series}
    day_rows, date_counts = {}, []
    for d in all_dates:
        out = []
        for code, rows in series.items():
            while ptr[code] + 1 < len(rows) and rows[ptr[code] + 1]["date"] <= d:
                ptr[code] += 1
            i = ptr[code]
            if i < 0:
                continue
            r = rows[i]
            fresh = (r["date"] == d)
            dc = daily_cross.get(code, [])
            k = bisect_right(daily_cross_dates.get(code, []), r["date"]) - 1
            lcx = lcd = ""
            if k >= 0:
                lcx = "G" if dc[k]["kind"] == "golden" else "D"
                lcd = dc[k]["kline_date"]
            out.append([code, r["close"], r["pct_chg"], r["diff"], r["dea"], r["hist"], r["state"],
                        rows[run_start[code][i]]["date"], i - run_start[code][i] + 1,
                        (r["prev_state"] or "") if fresh else "", (r["trigger"] or "") if fresh else "",
                        lcx, lcd, "" if fresh else r["date"]])
        day_rows[d] = out
        date_counts.append([d, sum(1 for x in out if x[13] == "")])   # 该日有真实 K 线的股票数

    # ---- 写分片 ----
    months = {}
    for d in all_dates:
        months.setdefault(d[:7].replace("-", ""), {})[d] = {"rows": day_rows[d], "crosses": crosses_by_date.get(d, [])}
    for ym, days in months.items():
        _write(os.path.join(out_dir, "macd", "days", f"{ym}.json"),
               {"schema_version": SCHEMA_VERSION, "fields": FIELDS, "days": days})
    latest = all_dates[-1] if all_dates else None
    if latest:
        _write(os.path.join(out_dir, "macd", "latest.json"),
               {"schema_version": SCHEMA_VERSION, "generated_at": gen, "date": latest, "fields": FIELDS,
                "rows": day_rows[latest], "crosses": crosses_by_date.get(latest, [])})

    # ---- 元信息与运行状态 ----
    lr = conn.execute("SELECT run_date,started_at,finished_at,n_total,n_ok,n_fail,detail FROM run_log ORDER BY id DESC LIMIT 1").fetchone()
    status = None
    if lr:
        status = {"run_date": lr["run_date"], "finished_at": lr["finished_at"], "n_total": lr["n_total"],
                  "n_ok": lr["n_ok"], "n_fail": lr["n_fail"], "failed": json.loads(lr["detail"] or "{}")}
    _write(os.path.join(out_dir, "macd", "meta.json"),
           {"schema_version": SCHEMA_VERSION, "generated_at": gen, "latest_date": latest,
            "dates": date_counts, "months": sorted(months), "states": STATE_RING,
            "default_ratio": DEFAULT_RATIO, "fields": FIELDS, "status": status})

    _write(os.path.join(out_dir, "stocks.json"),
           {"schema_version": SCHEMA_VERSION, "generated_at": gen,
            "stocks": [{"code": s["code"], "name": s["name"] or "", "market": s["market"], "focus": s["focus"],
                        "enabled": s["enabled"], "added_at": s["added_at"], "removed_at": s["removed_at"],
                        "has_data": s["code"] in has_data} for s in stocks]})
    conn.close()
    n_rows = sum(len(v) for v in day_rows.values())
    print(f"导出完成：{len(enabled)} 只启用股票，{len(all_dates)} 个日期，{n_rows} 行快照，"
          f"{len(months)} 个月分片；最新日期 {latest}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "data", "tech.db"))
    ap.add_argument("--out", default=os.path.join(HERE, "tech_api"))
    a = ap.parse_args()
    if not os.path.exists(a.db):
        sys.exit(f"数据库不存在: {a.db}")
    export(a.db, a.out)


if __name__ == "__main__":
    main()
