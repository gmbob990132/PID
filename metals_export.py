#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
金属模块 · 导出脚本（只读 metals.db，写 metals_api/ 下 JSON）

产出：
  metals_api/overview.json          概览：按金属分组(金/铜/锂)，每指标最新值+涨跌
                                     （hide_overview 的指标不进概览，如现货的最低/最高/涨跌）
  metals_api/series/<id>.json       近端序列（默认，load 快）
  metals_api/series/<id>.full.json  全量序列（选长区间才取）
                                     （hide_chart 的指标不导序列，省文件）
"""
import os
import sys
import json
import sqlite3
from datetime import datetime, timezone, timedelta

SCHEMA_VERSION = 1
CN_TZ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "data", "metals.db")
OUT_DIR = os.path.join(HERE, "metals_api")
RECENT_DAYS = 370
GROUP_NAME = {"gold": "金", "copper": "铜", "lithium": "锂"}
GROUP_ORDER = ["gold", "copper", "lithium"]


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
    conn.close()
    print(f"导出完成 → {out_dir}")
    print(f"  概览分组 {len(groups)}（{'/'.join(x['name'] for x in groups)}），序列 {n_series}×2 份")


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print(f"找不到 {DB_PATH}，请先运行 metals_ingest.py")
        sys.exit(1)
    export(DB_PATH, OUT_DIR)
