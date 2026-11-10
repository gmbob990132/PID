#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data/macro.db → macro_api/macro.json（单文件：全部序列 + 派生序列 + 沪深300月末叠加）"""
import os, json, sqlite3
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "macro.db")
CFG_PATH = os.path.join(HERE, "macro_config", "indicators.json")
OUT = os.path.join(HERE, "macro_api", "macro.json")
CN_TZ = timezone(timedelta(hours=8))


def load(conn, key):
    rows = conn.execute("SELECT date,value FROM macro_series WHERE key=? ORDER BY date", (key,)).fetchall()
    return {d: v for d, v in rows}


def main(db_path=DB_PATH, out=OUT):
    cfg = json.load(open(CFG_PATH, encoding="utf-8"))
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS macro_series(key TEXT, date TEXT, value REAL, PRIMARY KEY(key,date))")
    conn.execute("CREATE TABLE IF NOT EXISTS macro_log(key TEXT, ts TEXT, status TEXT, n INTEGER, msg TEXT)")
    raw, items = {}, []
    for ind in cfg["indicators"]:
        s = load(conn, ind["key"])
        raw[ind["key"]] = s
        st = conn.execute("SELECT status,msg FROM macro_log WHERE key=? ORDER BY ts DESC, rowid DESC LIMIT 1", (ind["key"],)).fetchone()
        items.append(dict(ind, dates=sorted(s), values=[round(s[d], 4) for d in sorted(s)],
                          status=(st[0] if st else "none"), error=(st[1] if st and st[0] != "ok" else "")))
    for dv in cfg.get("derived", []):
        a, b = raw.get(dv["a"], {}), raw.get(dv["b"], {})
        ds = sorted(set(a) & set(b))
        vals = [round(a[d] - b[d], 4) for d in ds] if dv["op"] == "sub" else []
        items.append(dict(dv, dates=ds, values=vals, status="ok" if ds else "none", error="", derived=True))
    h = load(conn, "hs300_close")
    for it in items:
        for k in ("adapter", "params", "sources", "a", "b", "op"):
            it.pop(k, None)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    data = {"generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"), "groups": cfg["groups"],
            "indicators": items, "hs300": {"dates": sorted(h), "values": [h[d] for d in sorted(h)]}}
    json.dump(data, open(out, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    n = sum(1 for i in items if i["dates"])
    print(f"宏观数据导出：{n}/{len(items)} 个序列有数据；沪深300叠加 {len(h)} 点 → {out}")
    return data


if __name__ == "__main__":
    main()
