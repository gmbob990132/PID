#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股权溢价指数模块 · 导出脚本（独立一套，只读 erp.db，写 JSON）

产出（写到仓库根 erp_api/ 下，与电解铝的 api/ 分开）：
  erp_api/overview.json              最新值卡片（点位/PE/ERP）
  erp_api/percentiles.json           PE、ERP 的近5年/近10年 20/50/80 分位（固定线）
  erp_api/series/<metric>.json       近端序列（默认近1年，load 快）
  erp_api/series/<metric>.full.json  全量序列（选长区间才取）
  erp_api/daily.json                 每天完整明细（点位/开高低收/涨跌/量额/PE），供图1悬停

分位口径：固定按 近5年 / 近10年 历史算，不随显示区间变（估值分位的通行做法）。
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

RECENT_DAYS = 370          # 近端文件≈近1年
CHART_METRICS = ["csi300_close", "csi300_pe_ttm", "csi300_erp"]
# 图1悬停要展示的完整明细（raw 字段 → 中文名）
DETAIL_FIELDS = [
    ("csi300_open", "开盘"), ("csi300_high", "最高"), ("csi300_low", "最低"),
    ("csi300_close", "收盘"), ("csi300_change", "涨跌"), ("csi300_pct", "涨跌幅"),
    ("csi300_vol", "成交量"), ("csi300_amount", "成交额"), ("csi300_pe_ttm", "市盈率TTM"),
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


def _percentile(sorted_vals, q):
    """线性插值分位数，q∈[0,1]。sorted_vals 已升序。"""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 < len(sorted_vals):
        return sorted_vals[lo] * (1 - frac) + sorted_vals[lo + 1] * frac
    return sorted_vals[lo]


def _percentiles_for(conn, metric_id, today):
    """对某指标，算近5年/近10年的 20/50/80 分位（各用该指标自己的历史）。"""
    out = {}
    for label, years in (("5y", 5), ("10y", 10)):
        cutoff = (today - timedelta(days=365 * years)).isoformat()
        vals = [v for (v,) in conn.execute(
            "SELECT value FROM observations WHERE metric_id=? AND obs_date>=? ",
            (metric_id, cutoff)).fetchall()]
        vals.sort()
        if vals:
            out[label] = {
                "p20": round(_percentile(vals, 0.20), 4),
                "p50": round(_percentile(vals, 0.50), 4),
                "p80": round(_percentile(vals, 0.80), 4),
                "n": len(vals),
            }
        else:
            out[label] = None
    return out


def export(db_path, out_dir):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    gen = _now_utc()
    today = datetime.now(CN_TZ).date()

    # ---- 序列：近端 + 全量 ----
    for mid in CHART_METRICS:
        pts = _series(conn, mid)
        _write(os.path.join(out_dir, "series", f"{mid}.json"),
               {"schema_version": SCHEMA_VERSION, "generated_at": gen,
                "metric": mid, "range": "recent", "points": pts[-RECENT_DAYS:]})
        _write(os.path.join(out_dir, "series", f"{mid}.full.json"),
               {"schema_version": SCHEMA_VERSION, "generated_at": gen,
                "metric": mid, "range": "full", "points": pts})

    # ---- 每天完整明细（供图1悬停）；近端一份 + 全量一份 ----
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT obs_date FROM observations WHERE metric_id='csi300_close' "
        "ORDER BY obs_date").fetchall()]
    detail = {}
    for fid, _cn in DETAIL_FIELDS:
        for d, v in conn.execute("SELECT obs_date,value FROM observations WHERE metric_id=?", (fid,)).fetchall():
            detail.setdefault(d, {})[fid] = v
    daily_full = [{"date": d, **detail.get(d, {})} for d in dates]
    _write(os.path.join(out_dir, "daily.json"),
           {"schema_version": SCHEMA_VERSION, "generated_at": gen,
            "fields": DETAIL_FIELDS, "rows": daily_full[-RECENT_DAYS:]})
    _write(os.path.join(out_dir, "daily.full.json"),
           {"schema_version": SCHEMA_VERSION, "generated_at": gen,
            "fields": DETAIL_FIELDS, "rows": daily_full})

    # ---- 分位（PE、ERP 各自近5年/近10年）----
    _write(os.path.join(out_dir, "percentiles.json"), {
        "schema_version": SCHEMA_VERSION, "generated_at": gen, "as_of": today.isoformat(),
        "csi300_pe_ttm": _percentiles_for(conn, "csi300_pe_ttm", today),
        "csi300_erp": _percentiles_for(conn, "csi300_erp", today),
    })

    # ---- 概览（最新值卡片）----
    def latest(mid):
        r = conn.execute("SELECT obs_date,value FROM observations WHERE metric_id=? "
                         "ORDER BY obs_date DESC LIMIT 1", (mid,)).fetchone()
        return {"date": r["obs_date"], "value": r["value"]} if r else None
    _write(os.path.join(out_dir, "overview.json"), {
        "schema_version": SCHEMA_VERSION, "generated_at": gen, "module": "erp", "name": "股权溢价指数",
        "metrics": [
            {"id": "csi300_close", "name": "沪深300", "unit": "点", "latest": latest("csi300_close")},
            {"id": "csi300_pe_ttm", "name": "市盈率TTM", "unit": "倍", "latest": latest("csi300_pe_ttm")},
            {"id": "csi300_erp", "name": "股权溢价指数", "unit": "%", "latest": latest("csi300_erp")},
        ],
    })
    conn.close()

    print(f"导出完成 → {out_dir}")
    print(f"  序列 {len(CHART_METRICS)}×2 份，明细 {len(daily_full)} 天，分位(PE/ERP × 5y/10y)，概览")


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print(f"找不到数据库 {DB_PATH}，请先运行 erp_ingest.py")
        sys.exit(1)
    export(DB_PATH, OUT_DIR)
