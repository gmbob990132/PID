#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
煤炭价格模块 · 导出（只读 data/coal.db，写 coal_api/coal.json，前端只读这个文件）

每条序列带：全部历史点、最新值、环比（较上一期）、同比（较去年同期最近的一期；历史不足则为 null）。
"""
import os
import sys
import json
import sqlite3
import statistics
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from coal_ingest import GROUPS, BLOCKS  # noqa: E402


def _d(s):
    return date.fromisoformat(s)


def _minus_year(d):
    try:
        return d.replace(year=d.year - 1)
    except ValueError:                                         # 2 月 29 日
        return d.replace(year=d.year - 1, day=28)


def _freq(dates):
    if len(dates) < 2:
        return "—", 7
    recent = dates[-13:]                       # 只看最近 12 个间隔：老数据可能更密（日度），不能拖累现在的判断
    gaps = [(_d(b) - _d(a)).days for a, b in zip(recent, recent[1:])]
    g = statistics.median(gaps)
    return ("周度" if g <= 10 else "月度" if g <= 40 else "低频"), g


def stats(points, site_delta_last):
    """points: [(date, value)] 升序 → (latest, mom, yoy, freq)"""
    ds = [p[0] for p in points]
    freq, gap = _freq(ds)
    ld, lv = points[-1]
    latest = {"date": ld, "value": lv}
    if len(points) >= 2:
        pd_, pv = points[-2]
    elif site_delta_last is not None:
        pd_, pv = None, lv - site_delta_last
    else:
        pd_, pv = None, None
    mom = None if pv is None else {"ref_date": pd_, "delta": round(lv - pv, 2), "pct": round((lv - pv) / pv * 100, 2) if pv else None}
    target = _minus_year(_d(ld))
    best = min(points, key=lambda p: abs((_d(p[0]) - target).days))
    yoy = None
    if abs((_d(best[0]) - target).days) <= max(4, gap * 0.75) and best[0] < ld:
        yoy = {"ref_date": best[0], "ref_value": best[1], "delta": round(lv - best[1], 2),
               "pct": round((lv - best[1]) / best[1] * 100, 2) if best[1] else None}
    return latest, mom, yoy, freq


def export(db_path, out_dir):
    conn = sqlite3.connect(db_path)
    sers = {r[0]: r for r in conn.execute("SELECT series,grp,block,title,label,unit,sort FROM coal_series ORDER BY sort")}
    groups = []
    for gk, gname in GROUPS:
        blocks = []
        for b in [b for b in BLOCKS if b["grp"] == gk]:
            ss = []
            for sid, (_, _g, blk, title, label, unit, _s) in sers.items():
                if blk != b["id"]:
                    continue
                rows = conn.execute("SELECT date,value,site_delta FROM coal_price WHERE series=? ORDER BY date", (sid,)).fetchall()
                if not rows:
                    continue
                pts = [(r[0], r[1]) for r in rows]
                latest, mom, yoy, freq = stats(pts, rows[-1][2])
                ss.append({"id": sid, "label": label, "points": [[d, v] for d, v in pts], "latest": latest, "mom": mom, "yoy": yoy, "freq": freq})
            if ss:
                blocks.append({"id": b["id"], "title": b["title"], "unit": b["unit"], "freq": ss[0]["freq"], "series": ss})
        groups.append({"key": gk, "name": gname, "blocks": blocks})
    runs = conn.execute("SELECT run_at,item,status,message FROM run_log ORDER BY id DESC LIMIT 40").fetchall()
    last_run = runs[0][0] if runs else None
    failed = [{"item": i, "message": m} for (t, i, s, m) in runs if s == "failed" and t == last_run]
    earliest = min((s["points"][0][0] for g in groups for b in g["blocks"] for s in b["series"]), default=None)
    latest_all = max((s["latest"]["date"] for g in groups for b in g["blocks"] for s in b["series"]), default=None)
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": "CCTD 中国煤炭市场网",
           "since": earliest, "latest": latest_all, "last_run": last_run, "failed": failed, "groups": groups}
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "coal.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    conn.close()
    n = sum(len(b["series"]) for g in groups for b in g["blocks"])
    print(f"导出完成 → {out_dir}/coal.json：{n} 条序列，最新 {latest_all}，本次失败 {len(failed)} 项")
    return out


def _selftest():
    import tempfile
    pts = [("2025-01-03", 707), ("2026-10-09", 767), ("2026-10-16", 770)]
    l, m, y, f = stats(pts, None)
    assert l == {"date": "2026-10-16", "value": 770} and m["delta"] == 3 and m["pct"] == 0.39
    assert y is None, "去年同期只有 2025-01-03，离 2025-10-16 太远，不应硬配"
    wk = [(f"2025-10-{d:02d}", 700 + d) for d in (3, 10, 17, 24)] + [("2026-10-16", 770)]
    l, m, y, f = stats(wk, None)
    assert y and y["ref_date"] == "2025-10-17" and y["delta"] == 770 - 717 and f == "周度", y
    mo = [("2025-11-01", 641), ("2025-12-01", 619)] + [(f"2026-{m:02d}-01", 600 + m) for m in range(1, 10)]
    l, m, y, f = stats(mo, None)
    assert y is None and f == "月度"
    old_daily = [(f"2024-{m:02d}-{d:02d}", 700) for m in (1, 2) for d in range(1, 29)] + wk
    l, m, y, f = stats(old_daily, None)
    assert f == "周度" and y and y["ref_date"] == "2025-10-17", (f, y)         # 前面一大段日度数据，不影响现在按周度判断
    l, m, y, f = stats([("2025-11-01", 641)], None)
    assert m is None
    l, m, y, f = stats([("2025-11-01", 641)], 10)
    assert m["delta"] == 10 and m["ref_date"] is None
    assert _minus_year(date(2028, 2, 29)) == date(2027, 2, 28)
    # 端到端：用 ingest 的自检库
    import coal_ingest as ci
    tmp = tempfile.mkdtemp(); db = os.path.join(tmp, "c.db")
    conn = sqlite3.connect(db); ci.init_db(conn)
    conn.execute("INSERT INTO coal_series VALUES ('qhd_zh_0','qhd','qhd_zh','综合交易价','5500大卡','元/吨',0)")
    conn.executemany("INSERT INTO coal_price VALUES ('qhd_zh_0',?,?,NULL)", wk); conn.commit(); conn.close()
    out = export(db, tmp)
    j = json.load(open(os.path.join(tmp, "coal.json"), encoding="utf-8"))
    assert j["groups"][0]["blocks"][0]["series"][0]["yoy"]["ref_date"] == "2025-10-17" and j["latest"] == "2026-10-16"
    assert [g["key"] for g in j["groups"]] == ["qhd", "origin", "hl"] and j["groups"][1]["blocks"] == []
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "data", "coal.db")
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "coal_api")
    if not os.path.exists(db):
        sys.exit(f"数据库不存在: {db}")
    export(db, out)
