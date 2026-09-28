#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
农产品模块 · 导出脚本（只读 agri.db，写 agri_api/）
产出：
  agri_api/overview.json                 概览：11 品种当前价+涨跌+20/50/80分位
  agri_api/series/<pid>.json (+.full)     各品种历史序列（近端/全量）
分位：按各品种可得历史算 20/50/80，历史超过10年则只用近10年。
"""
import os
import sys
import json
import sqlite3
from datetime import datetime, timezone, timedelta

SCHEMA_VERSION = 1
CN_TZ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "data", "agri.db")
OUT_DIR = os.path.join(HERE, "agri_api")
RECENT_DAYS = 370
MAX_YEARS = 10   # 分位最多取近10年


def _now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def _percentile(sv, q):
    if not sv:
        return None
    if len(sv) == 1:
        return sv[0]
    pos = q * (len(sv) - 1); lo = int(pos); fr = pos - lo
    return round(sv[lo] * (1 - fr) + sv[min(lo + 1, len(sv) - 1)] * fr, 2)


def export(db_path, out_dir):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    gen = _now_utc()
    today = datetime.now(CN_TZ).date()
    cutoff = (today - timedelta(days=365 * MAX_YEARS)).isoformat()

    metrics = conn.execute("SELECT id,name,unit FROM metrics WHERE active=1 ORDER BY sort_order").fetchall()
    cards = []
    for m in metrics:
        pid = m["id"]
        rows = conn.execute("SELECT obs_date,value FROM observations WHERE metric_id=? ORDER BY obs_date",
                            (pid,)).fetchall()
        pts = [{"date": r["obs_date"], "value": r["value"]} for r in rows]
        _write(os.path.join(out_dir, "series", f"{pid}.json"),
               {"schema_version": SCHEMA_VERSION, "generated_at": gen, "name": m["name"], "unit": m["unit"],
                "range": "recent", "points": pts[-RECENT_DAYS:]})
        _write(os.path.join(out_dir, "series", f"{pid}.full.json"),
               {"schema_version": SCHEMA_VERSION, "generated_at": gen, "name": m["name"], "unit": m["unit"],
                "range": "full", "points": pts})
        # 分位：近10年内的值
        vals = sorted(p["value"] for p in pts if p["date"] >= cutoff)
        p20 = _percentile(vals, .2); p50 = _percentile(vals, .5); p80 = _percentile(vals, .8)
        latest = pts[-1] if pts else None
        prev = pts[-2] if len(pts) >= 2 else None
        change = round(latest["value"] - prev["value"], 1) if (latest and prev) else None
        cards.append({"id": pid, "name": m["name"], "unit": m["unit"],
                      "latest": latest, "change": change,
                      "p20": p20, "p50": p50, "p80": p80, "n": len(vals)})

    _write(os.path.join(out_dir, "overview.json"),
           {"schema_version": SCHEMA_VERSION, "generated_at": gen, "module": "agri",
            "name": "农产品", "products": cards})
    conn.close()
    print(f"导出完成 → {out_dir}")
    print(f"  {len(cards)} 个品种：概览 + 序列×2 + 分位（近{MAX_YEARS}年内）")


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print(f"找不到 {DB_PATH}，请先运行 agri_ingest.py")
        sys.exit(1)
    export(DB_PATH, OUT_DIR)
