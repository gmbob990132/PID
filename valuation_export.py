#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指数估值模块 · 导出脚本（只读 valuation.db，写 valuation_api/）
每个指数导出：估值序列(PE/PB/PS 及中位数, 近端+全量) + 当前值卡 + 分位(5年/10年，科创50只5年)。
分位按各指数真实可得历史算：数据不足N年则该档为 null（不硬凑）。
"""
import os, sys, json, sqlite3, datetime

SCHEMA = 1
CN_TZ = datetime.timezone(datetime.timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "data", "valuation.db")
OUT_DIR = os.path.join(HERE, "valuation_api")
RECENT_DAYS = 370

INDEXES = [
    {"id": "csi300", "name": "沪深300", "years": [5, 10]},
    {"id": "cyb",    "name": "创业板指", "years": [5, 10]},
    {"id": "star50", "name": "科创50", "years": [5]},          # 只5年
]
# 画图/展示的估值指标（后缀 → 中文）；中位数、股息率也导，但图默认用加权
CHART_METRICS = [("pe", "PE-TTM"), ("pb", "PB"), ("ps", "PS-TTM")]
EXTRA_SERIES = [("pe_median", "PE中位数"), ("pb_median", "PB中位数"),
                ("ps_median", "PS中位数"), ("dyr", "股息率"), ("cp", "收盘点位")]
MIN_DAYS_PER_YEAR = 200   # 判定"够N年"的最低天数阈值（每年约240交易日，留余量）


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def _series(conn, mid):
    return [{"date": d, "value": v} for d, v in conn.execute(
        "SELECT obs_date,value FROM observations WHERE metric_id=? ORDER BY obs_date", (mid,)).fetchall()]


def _pct(sv, q):
    if not sv:
        return None
    if len(sv) == 1:
        return sv[0]
    pos = q * (len(sv) - 1); lo = int(pos); fr = pos - lo
    return round(sv[lo] * (1 - fr) + sv[min(lo + 1, len(sv) - 1)] * fr, 3)


def _percentiles(pts, today, years):
    """对一条序列，算指定 years 档的 20/50/80 分位；数据不足则 null。"""
    out = {}
    for y in years:
        cutoff = (today - datetime.timedelta(days=365 * y)).isoformat()
        vals = sorted(p["value"] for p in pts if p["date"] >= cutoff and p["value"] is not None)
        if len(vals) >= MIN_DAYS_PER_YEAR * y * 0.8:   # 够这么多年才算
            out[f"{y}y"] = {"p20": _pct(vals, .2), "p50": _pct(vals, .5), "p80": _pct(vals, .8), "n": len(vals),
                            "cur_pos": round(_cur_pos(vals, pts[-1]["value"]), 3) if pts else None}
        else:
            out[f"{y}y"] = None
    return out


def _cur_pos(sorted_vals, cur):
    """当前值在历史中的分位（0~1）。"""
    if not sorted_vals or cur is None:
        return 0
    below = sum(1 for v in sorted_vals if v <= cur)
    return below / len(sorted_vals)


def export(db_path, out_dir):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    gen = _now(); today = datetime.datetime.now(CN_TZ).date()
    idx_list = []

    for idx in INDEXES:
        pfx = idx["id"]; base = os.path.join(out_dir, pfx)
        # 序列：主估值 + 附加，各近端+全量
        for suf, _nm in CHART_METRICS + EXTRA_SERIES:
            pts = _series(conn, f"{pfx}_{suf}")
            _write(os.path.join(base, "series", f"{suf}.json"),
                   {"schema_version": SCHEMA, "generated_at": gen, "metric": suf, "range": "recent", "points": pts[-RECENT_DAYS:]})
            _write(os.path.join(base, "series", f"{suf}.full.json"),
                   {"schema_version": SCHEMA, "generated_at": gen, "metric": suf, "range": "full", "points": pts})

        # 分位（每个主估值指标，按该指数的 years 档）
        perc = {}
        for suf, _nm in CHART_METRICS:
            pts = _series(conn, f"{pfx}_{suf}")
            perc[suf] = _percentiles(pts, today, idx["years"])
        _write(os.path.join(base, "percentiles.json"),
               {"schema_version": SCHEMA, "generated_at": gen, "years": idx["years"], "metrics": perc})

        # 当前值卡：各指标最新值
        def latest(suf):
            r = conn.execute("SELECT obs_date,value FROM observations WHERE metric_id=? ORDER BY obs_date DESC LIMIT 1",
                             (f"{pfx}_{suf}",)).fetchone()
            return {"date": r["obs_date"], "value": r["value"]} if r else None
        cards = {suf: latest(suf) for suf, _ in CHART_METRICS + EXTRA_SERIES}
        _write(os.path.join(base, "overview.json"),
               {"schema_version": SCHEMA, "generated_at": gen, "index": pfx, "name": idx["name"],
                "years": idx["years"], "latest": cards})

        idx_list.append({"id": pfx, "name": idx["name"], "years": idx["years"]})

    _write(os.path.join(out_dir, "indexes.json"),
           {"schema_version": SCHEMA, "generated_at": gen, "indexes": idx_list})
    conn.close()
    print(f"导出完成 → {out_dir}")
    print(f"  {len(INDEXES)} 指数（{'/'.join(x['name'] for x in INDEXES)}），各：估值序列 + 当前值 + 分位")


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print(f"找不到 {DB_PATH}，请先运行 valuation_ingest.py"); sys.exit(1)
    export(DB_PATH, OUT_DIR)
