#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股权溢价指数模块 · 导出脚本（多指数：沪深300 + 科创50）

产出（每个指数一套，放到 erp_api/{指数id}/ 下）：
  erp_api/{idx}/series/{close|pe_ttm|erp}.json (+.full.json)
  erp_api/{idx}/daily.json (+.full.json)        每天完整明细（供图1悬停）
  erp_api/{idx}/percentiles.json                PE、ERP 的近5年/近10年 20/50/80 分位
  erp_api/indexes.json                          指数清单（前端据此建二级 tab）

分位口径：固定按 近5年 / 近10年 历史算，不随显示区间变。
"""
import os
import sys
import json
import sqlite3
from datetime import datetime, timezone, timedelta

SCHEMA_VERSION = 1
CN_TZ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "data", "erp.db")
OUT_DIR = os.path.join(HERE, "erp_api")
RECENT_DAYS = 370

# 指数清单（与 ingest 一致）：id / 中文名
INDEXES = [
    {"id": "csi300", "name": "沪深300"},
    {"id": "star50", "name": "科创50"},
]
# 图1悬停明细字段（后缀 → 中文名）
DETAIL_SUFFIX = [
    ("open", "开盘"), ("high", "最高"), ("low", "最低"), ("close", "收盘"),
    ("change", "涨跌"), ("pct", "涨跌幅"), ("vol", "成交量"), ("amount", "成交额"),
    ("pe_ttm", "市盈率TTM"),
]


def _now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def _series(conn, metric_id):
    rows = conn.execute("SELECT obs_date, value FROM observations WHERE metric_id=? "
                        "ORDER BY obs_date", (metric_id,)).fetchall()
    return [{"date": d, "value": v} for d, v in rows]


def _percentile(sv, q):
    if not sv:
        return None
    if len(sv) == 1:
        return sv[0]
    pos = q * (len(sv) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 < len(sv):
        return sv[lo] * (1 - frac) + sv[lo + 1] * frac
    return sv[lo]


def _percentiles_for(conn, metric_id, today):
    out = {}
    for label, years in (("5y", 5), ("10y", 10)):
        cutoff = (today - timedelta(days=365 * years)).isoformat()
        vals = [v for (v,) in conn.execute(
            "SELECT value FROM observations WHERE metric_id=? AND obs_date>=?",
            (metric_id, cutoff)).fetchall()]
        vals.sort()
        out[label] = ({"p20": round(_percentile(vals, .2), 4), "p50": round(_percentile(vals, .5), 4),
                       "p80": round(_percentile(vals, .8), 4), "n": len(vals)} if vals else None)
    return out


def export(db_path, out_dir):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    gen = _now_utc()
    today = datetime.now(CN_TZ).date()

    idx_meta = []
    for idx in INDEXES:
        pfx = idx["id"]
        base = os.path.join(out_dir, pfx)

        # 序列：点位 / PE / ERP，各近端+全量
        for suf in ("close", "pe_ttm", "erp"):
            pts = _series(conn, f"{pfx}_{suf}")
            _write(os.path.join(base, "series", f"{suf}.json"),
                   {"schema_version": SCHEMA_VERSION, "generated_at": gen, "metric": f"{pfx}_{suf}",
                    "range": "recent", "points": pts[-RECENT_DAYS:]})
            _write(os.path.join(base, "series", f"{suf}.full.json"),
                   {"schema_version": SCHEMA_VERSION, "generated_at": gen, "metric": f"{pfx}_{suf}",
                    "range": "full", "points": pts})

        # 每天明细（供图1悬停）
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT obs_date FROM observations WHERE metric_id=? ORDER BY obs_date",
            (f"{pfx}_close",)).fetchall()]
        detail = {}
        for suf, _cn in DETAIL_SUFFIX:
            for d, v in conn.execute("SELECT obs_date,value FROM observations WHERE metric_id=?",
                                     (f"{pfx}_{suf}",)).fetchall():
                detail.setdefault(d, {})[suf] = v
        rows = [{"date": d, **detail.get(d, {})} for d in dates]
        fields = [[suf, cn] for suf, cn in DETAIL_SUFFIX]
        _write(os.path.join(base, "daily.json"),
               {"schema_version": SCHEMA_VERSION, "generated_at": gen, "fields": fields, "rows": rows[-RECENT_DAYS:]})
        _write(os.path.join(base, "daily.full.json"),
               {"schema_version": SCHEMA_VERSION, "generated_at": gen, "fields": fields, "rows": rows})

        # 分位（PE、ERP 各近5年/近10年）
        _write(os.path.join(base, "percentiles.json"), {
            "schema_version": SCHEMA_VERSION, "generated_at": gen, "as_of": today.isoformat(),
            "pe_ttm": _percentiles_for(conn, f"{pfx}_pe_ttm", today),
            "erp": _percentiles_for(conn, f"{pfx}_erp", today),
        })

        # 最新值（给指数清单顺带带上）
        lat = {}
        for suf in ("close", "pe_ttm", "erp"):
            r = conn.execute("SELECT obs_date,value FROM observations WHERE metric_id=? "
                             "ORDER BY obs_date DESC LIMIT 1", (f"{pfx}_{suf}",)).fetchone()
            lat[suf] = ({"date": r["obs_date"], "value": r["value"]} if r else None)
        idx_meta.append({"id": pfx, "name": idx["name"], "latest": lat})

    _write(os.path.join(out_dir, "indexes.json"),
           {"schema_version": SCHEMA_VERSION, "generated_at": gen, "indexes": idx_meta})
    conn.close()
    print(f"导出完成 → {out_dir}")
    print(f"  指数 {len(INDEXES)}（{'/'.join(x['name'] for x in INDEXES)}），每个：序列3×2 + 明细 + 分位")


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print(f"找不到数据库 {DB_PATH}，请先运行 erp_ingest.py")
        sys.exit(1)
    export(DB_PATH, OUT_DIR)
