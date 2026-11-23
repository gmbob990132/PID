#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
金属模块 · 导出脚本（只读 metals.db，写 metals_api/ 下 JSON）

产出：
  metals_api/overview.json          概览：按金属分组(金/铜/锂)，每指标最新值+涨跌
                                     （hide_overview 的指标不进概览，如现货的最低/最高/涨跌）
  metals_api/series/<id>.json       近端序列（默认，load 快）
  metals_api/series/<id>.full.json  全量序列（选长区间才取）
  metals_api/ratios.json            金银比（COMEX金÷COMEX银）、金铜比（COMEX金 美元/盎司 ÷ LME铜 美元/磅）
                                     全历史序列 + 近5年/近10年百分位 + 近10年 20/50/80 分位线
                                     （hide_chart 的指标不导序列，省文件）
"""
import os
import sys
import json
import sqlite3
from datetime import datetime, timezone, timedelta, date

SCHEMA_VERSION = 1
CN_TZ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "data", "metals.db")
OUT_DIR = os.path.join(HERE, "metals_api")
RECENT_DAYS = 370
GROUP_NAME = {"gold": "金", "silver": "银", "copper": "铜", "lithium": "锂"}
GROUP_ORDER = ["gold", "silver", "copper", "lithium"]
LB_PER_TONNE = 2204.62262
RATIOS = [  # id, 名称, 分子, 分母, 分母换算, 小数位, 说明
    ("gs", "金银比", "au_comex", "ag_comex", 1.0, 2, "COMEX金 ÷ COMEX银（同为美元/盎司）：1 盎司黄金可换多少盎司白银"),
    ("gc", "金铜比", "au_comex", "cu_lme", 1.0 / LB_PER_TONNE, 1, "COMEX金（美元/盎司）÷ LME铜（换算成美元/磅）：1 盎司黄金可换多少磅铜"),
]


def _cut(d_last, years):
    y, m, d = map(int, d_last.split("-"))
    try:
        return date(y - years, m, d).isoformat()
    except ValueError:
        return date(y - years, m, 28).isoformat()


def _pos(vals, x):
    return round(sum(1 for v in vals if v <= x) / len(vals), 4) if vals else None


def _quant(vals, p):
    s = sorted(vals)
    k = (len(s) - 1) * p
    f = int(k)
    return s[f] if f + 1 >= len(s) else s[f] + (s[f + 1] - s[f]) * (k - f)


def ratio_block(num_pts, den_pts, factor, dp):
    """两条序列按同日对齐求比值 → 序列 + 百分位；num/den: {日期: 值}"""
    pts = [(d, num_pts[d] / (den_pts[d] * factor)) for d in sorted(num_pts) if d in den_pts and den_pts[d]]
    if len(pts) < 2:
        return None
    last_d, cur = pts[-1]
    w5 = [v for d, v in pts if d >= _cut(last_d, 5)]
    w10 = [v for d, v in pts if d >= _cut(last_d, 10)]
    base = w10 if len(w10) >= 20 else [v for _, v in pts]
    return {"dates": [d for d, _ in pts], "values": [round(v, dp + 2) for _, v in pts],
            "latest": {"date": last_d, "value": round(cur, dp), "change": round(cur - pts[-2][1], dp)},
            "pct_5y": _pos(w5, cur), "pct_10y": _pos(w10, cur),
            "q10y": {"p20": round(_quant(base, .2), dp), "p50": round(_quant(base, .5), dp), "p80": round(_quant(base, .8), dp)},
            "since": pts[0][0], "dp": dp}


def _now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def export(db_path, out_dir):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    gen = _now_utc()

    metrics = conn.execute(
        "SELECT id,name,grp,unit,hide_overview,hide_chart FROM metrics "
        "WHERE active=1 ORDER BY sort_order").fetchall()

    # ---- 序列：非 hide_chart 的导出近端+全量 ----
    n_series = 0
    for m in metrics:
        if m["hide_chart"]:
            continue
        rows = conn.execute("SELECT obs_date,value FROM observations WHERE metric_id=? "
                            "ORDER BY obs_date", (m["id"],)).fetchall()
        pts = [{"date": r["obs_date"], "value": r["value"]} for r in rows]
        meta = {"id": m["id"], "name": m["name"], "unit": m["unit"]}
        _write(os.path.join(out_dir, "series", f"{m['id']}.json"),
               {"schema_version": SCHEMA_VERSION, "generated_at": gen, "metric": meta,
                "range": "recent", "points": pts[-RECENT_DAYS:]})
        _write(os.path.join(out_dir, "series", f"{m['id']}.full.json"),
               {"schema_version": SCHEMA_VERSION, "generated_at": gen, "metric": meta,
                "range": "full", "points": pts})
        n_series += 1

    # ---- 概览：按金属分组 ----
    def latest(mid):
        r = conn.execute("SELECT obs_date,value FROM observations WHERE metric_id=? "
                         "ORDER BY obs_date DESC LIMIT 1", (mid,)).fetchone()
        if not r:
            return None
        prev = conn.execute("SELECT value FROM observations WHERE metric_id=? AND obs_date<? "
                            "ORDER BY obs_date DESC LIMIT 1", (mid, r["obs_date"])).fetchone()
        change = round(r["value"] - prev["value"], 4) if prev else None
        return {"date": r["obs_date"], "value": r["value"], "change": change}

    groups = []
    for g in GROUP_ORDER:
        gm = [m for m in metrics if m["grp"] == g]
        if not gm:
            continue
        groups.append({
            "id": g, "name": GROUP_NAME.get(g, g),
            "metrics": [{"id": m["id"], "name": m["name"], "unit": m["unit"],
                         "hide_overview": bool(m["hide_overview"]), "hide_chart": bool(m["hide_chart"]),
                         "latest": latest(m["id"])}
                        for m in gm],
        })
    _write(os.path.join(out_dir, "overview.json"), {
        "schema_version": SCHEMA_VERSION, "generated_at": gen,
        "module": "metals", "name": "金属", "metals": groups,
    })
    # ---- 比价：金银比 / 金铜比 ----
    def allpts(mid):
        return {r[0]: r[1] for r in conn.execute(
            "SELECT obs_date,value FROM observations WHERE metric_id=? AND value IS NOT NULL", (mid,))}
    ratios = {}
    for rid, nm, a, b, f, dp, note in RATIOS:
        blk = ratio_block(allpts(a), allpts(b), f, dp)
        if blk:
            ratios[rid] = {"id": rid, "name": nm, "note": note, **blk}
    _write(os.path.join(out_dir, "ratios.json"), {"schema_version": SCHEMA_VERSION, "generated_at": gen, "ratios": ratios})
    conn.close()
    print(f"导出完成 → {out_dir}")
    for r in ratios.values():
        print(f"  {r['name']} 最新 {r['latest']['date']} = {r['latest']['value']}（{r['since']} 起，近10年百分位 {r['pct_10y']}）")
    print(f"  概览分组 {len(groups)}（{'/'.join(x['name'] for x in groups)}），序列 {n_series}×2 份")


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print(f"找不到 {DB_PATH}，请先运行 metals_ingest.py")
        sys.exit(1)
    export(DB_PATH, OUT_DIR)
