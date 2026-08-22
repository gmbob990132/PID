#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A-3 导出脚本：把库里的数据按数据契约导出成前端要的 JSON（阶段1：静态文件）。
只读库、写 JSON，不碰网络（读写分离）。文件路径对应将来的 API 路径，
阶段2 换成真 API 时前端只改请求地址、JSON 形状不变。

用法：
    python3 export_json.py                    # 读 data/portfolio.db → 写 api/
    python3 export_json.py data/selftest.db   # 指定库

产出（对应契约）：
    api/modules.json              →  将来的 GET /api/modules
    api/overview/<module>.json    →  GET /api/overview/{module}
    api/series/<metric>.json      →  GET /api/metrics/{id}/series
    api/status.json               →  GET /api/status
"""
import os
import sys
import json
import sqlite3
from datetime import datetime, timezone, timedelta

SCHEMA_VERSION = 1
CN_TZ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "data", "portfolio.db")
OUT_DIR = os.path.join(HERE, "api")

# 每个点带自算环比（LAG）；口径归我们自己
SERIES_SQL = """
SELECT obs_date, value, src_change,
       value - LAG(value) OVER (ORDER BY obs_date) AS mom
FROM observations WHERE metric_id = ? ORDER BY obs_date;
"""
YOY_SQL = """
SELECT t.value - (
    SELECT y.value FROM observations y
    WHERE y.metric_id = t.metric_id AND y.obs_date <= date(t.obs_date, '-1 year')
    ORDER BY y.obs_date DESC LIMIT 1) AS yoy
FROM observations t WHERE t.metric_id = ? ORDER BY t.obs_date DESC LIMIT 1;
"""


def _now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _last_ingest(conn, metric_id):
    r = conn.execute(
        "SELECT run_at,status,rows_written FROM ingest_runs WHERE metric_id=? "
        "ORDER BY id DESC LIMIT 1", (metric_id,)).fetchone()
    return dict(r) if r else None


def export(db_path, out_dir):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    today = datetime.now(CN_TZ).date().isoformat()
    gen = _now_utc()

    modules = conn.execute(
        "SELECT id,name,sort_order FROM modules WHERE active=1 ORDER BY sort_order").fetchall()

    modules_out = []
    status_sources = []
    counts = {"ok": 0, "no_data": 0, "failed": 0}

    for mod in modules:
        metrics = conn.execute(
            "SELECT id,name,category,unit,source_type,source_ref,update_freq "
            "FROM metrics WHERE module_id=? AND active=1 ORDER BY sort_order",
            (mod["id"],)).fetchall()

        # modules.json 里带指标定义（前端据此画 tab / 子tab / 卡）
        cats = []
        for m in metrics:
            if m["category"] not in cats:
                cats.append(m["category"])
        modules_out.append({
            "id": mod["id"], "name": mod["name"], "categories": cats,
            "metrics": [{"id": m["id"], "name": m["name"], "category": m["category"],
                         "unit": m["unit"], "source_type": m["source_type"],
                         "source_ref": m["source_ref"], "update_freq": m["update_freq"]}
                        for m in metrics],
        })

        overview_metrics = []
        for m in metrics:
            rows = conn.execute(SERIES_SQL, (m["id"],)).fetchall()
            yoy_row = conn.execute(YOY_SQL, (m["id"],)).fetchone()
            yoy = yoy_row["yoy"] if yoy_row else None
            li = _last_ingest(conn, m["id"])

            # series 文件：图用 date+value，表用 change(自算环比)
            points = [{"date": r["obs_date"], "value": r["value"],
                       "change": r["mom"], "src_change": r["src_change"]} for r in rows]
            _write(os.path.join(out_dir, "series", f"{m['id']}.json"), {
                "schema_version": SCHEMA_VERSION, "generated_at": gen,
                "metric": {"id": m["id"], "name": m["name"], "category": m["category"],
                           "unit": m["unit"]},
                "points": points,
            })

            latest = rows[-1] if rows else None
            overview_metrics.append({
                "id": m["id"], "name": m["name"], "category": m["category"], "unit": m["unit"],
                "source_type": m["source_type"], "update_freq": m["update_freq"],
                "latest": None if not latest else {
                    "date": latest["obs_date"], "value": latest["value"],
                    "mom": latest["mom"], "yoy": yoy},
                "updated_today": bool(latest and latest["obs_date"] == today),
                "last_ingest": li,
            })

            # status 汇总
            st = (li or {}).get("status", "unknown")
            counts[st] = counts.get(st, 0) + 1
            status_sources.append({
                "metric_id": m["id"], "name": m["name"],
                "latest_date": latest["obs_date"] if latest else None,
                "updated_today": bool(latest and latest["obs_date"] == today),
                "last_run": (li or {}).get("run_at"), "status": st,
            })

        _write(os.path.join(out_dir, "overview", f"{mod['id']}.json"), {
            "schema_version": SCHEMA_VERSION, "generated_at": gen,
            "module": mod["id"], "today": today, "metrics": overview_metrics,
        })

    _write(os.path.join(out_dir, "modules.json"), {
        "schema_version": SCHEMA_VERSION, "generated_at": gen, "modules": modules_out,
    })
    _write(os.path.join(out_dir, "status.json"), {
        "schema_version": SCHEMA_VERSION, "generated_at": gen,
        "today": today, "summary": counts, "sources": status_sources,
    })
    conn.close()

    # 屏幕反馈
    n_metrics = sum(len(m["metrics"]) for m in modules_out)
    print(f"导出完成 → {out_dir}")
    print(f"  modules.json（{len(modules_out)} 个模块，{n_metrics} 个指标）")
    print(f"  overview/*.json（{len(modules_out)} 个）")
    print(f"  series/*.json（{n_metrics} 个）")
    print(f"  status.json（ok={counts.get('ok',0)} no_data={counts.get('no_data',0)} failed={counts.get('failed',0)}）")


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print(f"找不到数据库 {DB_PATH}，请先运行 ingest.py")
        sys.exit(1)
    export(DB_PATH, OUT_DIR)
