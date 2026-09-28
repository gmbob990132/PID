#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
农产品模块 · 落库脚本（独立一套）
11 个品种主力连续（akshare futures_main_sina）→ SQLite。增量补齐、幂等。
用法：
  python3 agri_ingest.py            # 联网抓取/补齐 → data/agri.db
  python3 agri_ingest.py --selftest # 离线（akshare 会失败，仅验证建表/流程）
"""
import os
import sys
import sqlite3
from datetime import datetime, date, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "agri.db")
BACKFILL_START = date(2015, 1, 1)
OVERLAP_DAYS = 7
_SELFTEST = False

MODULE = {"id": "agri", "name": "农产品", "sort_order": 4}
# 品种：id / 中文名 / 主力连续代码 / 单位
PRODUCTS = [
    ("sugar",   "白糖",   "SR0", "元/吨"),
    ("cotton",  "棉花",   "CF0", "元/吨"),
    ("apple",   "苹果",   "AP0", "元/吨"),
    ("jujube",  "红枣",   "CJ0", "元/吨"),
    ("peanut",  "花生",   "PK0", "元/吨"),
    ("palm",    "棕榈油", "P0",  "元/吨"),
    ("corn",    "玉米",   "C0",  "元/吨"),
    ("starch",  "玉米淀粉", "CS0", "元/吨"),
    ("soymeal", "豆粕",   "M0",  "元/吨"),
    ("egg",     "鸡蛋",   "JD0", "元/吨"),
    ("hog",     "生猪",   "LH0", "元/吨"),
]


def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS modules(
      id TEXT PRIMARY KEY, name TEXT, sort_order INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS metrics(
      id TEXT PRIMARY KEY, module_id TEXT, name TEXT, unit TEXT, code TEXT,
      sort_order INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS observations(
      metric_id TEXT, obs_date TEXT, value REAL, source TEXT, ingested_at TEXT,
      PRIMARY KEY(metric_id, obs_date));
    CREATE TABLE IF NOT EXISTS ingest_runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, metric_id TEXT, run_at TEXT,
      status TEXT, rows_written INTEGER DEFAULT 0, message TEXT);
    """)
    conn.execute("INSERT INTO modules(id,name,sort_order) VALUES(?,?,?) "
                 "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                 (MODULE["id"], MODULE["name"], MODULE["sort_order"]))
    for i, (pid, nm, code, unit) in enumerate(PRODUCTS):
        conn.execute("INSERT INTO metrics(id,module_id,name,unit,code,sort_order) "
                     "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                     "name=excluded.name, unit=excluded.unit, code=excluded.code",
                     (pid, MODULE["id"], nm, unit, code, i))
    conn.commit()


def latest_date(conn, mid):
    r = conn.execute("SELECT MAX(obs_date) FROM observations WHERE metric_id=?", (mid,)).fetchone()
    return r[0] if r and r[0] else None


def upsert(conn, mid, d, v, now_utc):
    if v is None:
        return 0
    conn.execute("INSERT INTO observations(metric_id,obs_date,value,source,ingested_at) "
                 "VALUES(?,?,?,?,?) ON CONFLICT(metric_id,obs_date) DO UPDATE SET "
                 "value=excluded.value, ingested_at=excluded.ingested_at",
                 (mid, d, v, "akshare/futures_main_sina", now_utc))
    return 1


def fetch(code, start_d):
    import akshare as ak
    df = ak.futures_main_sina(symbol=code, start_date=start_d.strftime("%Y%m%d"), end_date="22220101")
    ccol = "收盘价" if "收盘价" in df.columns else next(c for c in df.columns if "close" in str(c).lower())
    dcol = "日期" if "日期" in df.columns else next(c for c in df.columns if "date" in str(c).lower())
    out = []
    for _, r in df.iterrows():
        try:
            v = float(r[ccol])
        except (TypeError, ValueError):
            continue
        out.append((str(r[dcol])[:10], v))
    return out


def run(db_path):
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = datetime.now(CN_TZ).date()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        first = (latest_date(conn, PRODUCTS[0][0]) is None)
        all_ok = True
        for pid, nm, code, unit in PRODUCTS:
            last = latest_date(conn, pid)
            start_d = (datetime.strptime(last, "%Y-%m-%d").date() - timedelta(days=OVERLAP_DAYS)) if last else BACKFILL_START
            start_d = max(start_d, BACKFILL_START)
            try:
                rows = [] if _SELFTEST else fetch(code, start_d)
                n = 0
                for d, v in rows:
                    n += upsert(conn, pid, d, v, now_utc)
                conn.execute("INSERT INTO ingest_runs(metric_id,run_at,status,rows_written,message) "
                             "VALUES(?,?,?,?,?)", (pid, now_utc, "ok" if n else "no_data", n, f"from {start_d}"))
            except Exception as ex:
                all_ok = False
                print(f"!! [{nm} {code}] 失败：{repr(ex)[:150]}")
                conn.execute("INSERT INTO ingest_runs(metric_id,run_at,status,rows_written,message) "
                             "VALUES(?,?,?,?,?)", (pid, now_utc, "failed", 0, repr(ex)[:180]))
        conn.commit()

        mode = "首次回填" if first else "增量补齐"
        print(f"[{datetime.now(CN_TZ):%Y-%m-%d %H:%M} CST] {mode}完成 → {db_path}")
        for pid, nm, code, unit in PRODUCTS:
            r = conn.execute("SELECT obs_date,value FROM observations WHERE metric_id=? "
                             "ORDER BY obs_date DESC LIMIT 1", (pid,)).fetchone()
            tot = conn.execute("SELECT COUNT(*) FROM observations WHERE metric_id=?", (pid,)).fetchone()[0]
            if r:
                print(f"  ✓ {nm:<6} {r[1]:>9.1f} {unit}  最新 {r[0]}  （共{tot}天）")
            else:
                print(f"  · {nm:<6} 无数据")
        return 0 if all_ok else 2
    finally:
        conn.close()


def main():
    global _SELFTEST
    if "--selftest" in sys.argv:
        _SELFTEST = True
        db = os.path.join(HERE, "data", "agri_selftest.db")
        if os.path.exists(db):
            os.remove(db)
        print("[自测] 仅验证建表/流程（akshare 离线不取数）\n")
        sys.exit(run(db))
    print(f"[{datetime.now(CN_TZ):%Y-%m-%d %H:%M} CST] 开始 …")
    sys.exit(run(DB_PATH))


if __name__ == "__main__":
    main()
