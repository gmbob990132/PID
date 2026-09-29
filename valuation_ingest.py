#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指数估值模块 · 落库脚本（理杏仁）
三个指数（沪深300/科创50/创业板指）的 PE/PB/PS(加权+中位数)/股息率/点位/成交额/融资余额/市值 每日历史。

- 首次：从 START_YEAR 起，按指数、按年分段取（接口：传 startDate 时只能一个指数）。
- 增量：每个指标查库里最新日期，只补之后的（重叠3天，幂等）。
- 可中断续跑：库里已有的不重取。

token 走环境变量，不写进代码：
    export LIXINGER_TOKEN=你的token
    python3 valuation_ingest.py            # 取到今天（首次会回填十年，分段进行）
    python3 valuation_ingest.py --selftest # 离线，用模拟数据验证落库/分段/增量逻辑
"""
import os, sys, json, gzip, sqlite3, time, datetime, urllib.request, urllib.error

CN_TZ = datetime.timezone(datetime.timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "valuation.db")
START_YEAR = 2016            # 十年起点（可改）；实际各指数从有数据那天起
OVERLAP_DAYS = 10
TOKEN = os.environ.get("LIXINGER_TOKEN")
BASE = "https://open.lixinger.com/api/cn/index/fundamental"
HEADERS = {"Content-Type": "application/json", "Accept-Encoding": "gzip"}
_SELFTEST = False

MODULE = {"id": "valuation", "name": "指数估值", "sort_order": 2}
INDEXES = [
    {"id": "csi300", "code": "000300", "name": "沪深300"},
    {"id": "star50", "code": "000688", "name": "科创50"},
    {"id": "cyb",    "code": "399006", "name": "创业板指"},
]
# 理杏仁 metric → (指标后缀, 中文名)。metric_id = {指数id}_{后缀}
METRICS = [
    ("pe_ttm.mcw",    "pe",        "PE-TTM"),
    ("pe_ttm.median", "pe_median", "PE中位数"),
    ("pb.mcw",        "pb",        "PB"),
    ("pb.median",     "pb_median", "PB中位数"),
    ("ps_ttm.mcw",    "ps",        "PS-TTM"),
    ("ps_ttm.median", "ps_median", "PS中位数"),
    ("dyr.mcw",       "dyr",       "股息率"),
    ("cp",            "cp",        "收盘点位"),
    ("ta",            "ta",        "成交额"),
    ("fb",            "fb",        "融资余额"),
    ("mc",            "mc",        "总市值"),
]
LXR_KEYS = [m[0] for m in METRICS]


def _read(resp):
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8")


def lxr_fetch(code, start_d, end_d):
    """取一个指数 [start,end] 的全指标每日历史，返回 [{date, <lxrkey>:val,...}]。"""
    body = {"token": TOKEN, "startDate": start_d.isoformat(), "endDate": end_d.isoformat(),
            "stockCodes": [code], "metricsList": LXR_KEYS}
    req = urllib.request.Request(BASE, data=json.dumps(body).encode(), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=40) as r:
        res = json.loads(_read(r))
    if not (isinstance(res, dict) and res.get("code") == 1):
        raise ValueError("理杏仁返回异常：" + json.dumps(res, ensure_ascii=False)[:200])
    return res.get("data", [])


def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS modules(id TEXT PRIMARY KEY, name TEXT, sort_order INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS metrics(id TEXT PRIMARY KEY, module_id TEXT, index_id TEXT, name TEXT, unit TEXT,
      sort_order INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS observations(metric_id TEXT, obs_date TEXT, value REAL, source TEXT, ingested_at TEXT,
      PRIMARY KEY(metric_id, obs_date));
    CREATE TABLE IF NOT EXISTS ingest_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, metric_id TEXT, run_at TEXT,
      status TEXT, rows_written INTEGER DEFAULT 0, message TEXT);
    """)
    conn.execute("INSERT INTO modules(id,name,sort_order) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                 (MODULE["id"], MODULE["name"], MODULE["sort_order"]))
    i = 0
    for idx in INDEXES:
        for (_lk, suf, nm) in METRICS:
            conn.execute("INSERT INTO metrics(id,module_id,index_id,name,unit,sort_order) VALUES(?,?,?,?,?,?) "
                         "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                         (f"{idx['id']}_{suf}", MODULE["id"], idx["id"], f"{idx['name']}{nm}", "", i)); i += 1
    conn.commit()


def latest_date(conn, mid):
    r = conn.execute("SELECT MAX(obs_date) FROM observations WHERE metric_id=?", (mid,)).fetchone()
    return r[0] if r and r[0] else None


def upsert(conn, mid, d, v, now):
    if v is None:
        return 0
    conn.execute("INSERT INTO observations(metric_id,obs_date,value,source,ingested_at) VALUES(?,?,?,?,?) "
                 "ON CONFLICT(metric_id,obs_date) DO UPDATE SET value=excluded.value, ingested_at=excluded.ingested_at",
                 (mid, d, v, "lixinger", now)); return 1


def run(db_path):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    today = datetime.datetime.now(CN_TZ).date()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        for idx in INDEXES:
            pfx = idx["id"]
            # 起点：以该指数 pe 的库内最新日期为准（重叠几天）；无则从 START_YEAR
            last = latest_date(conn, f"{pfx}_pe")
            start_d = (datetime.date.fromisoformat(last) - datetime.timedelta(days=OVERLAP_DAYS)) if last \
                else datetime.date(START_YEAR, 1, 1)
            print(f"—— {idx['name']}（{idx['code']}）从 {start_d} 起 ——")
            seg_start = start_d
            total = 0
            while seg_start <= today:
                seg_end = min(datetime.date(seg_start.year, 12, 31), today)
                if _SELFTEST:
                    rows = _sample(pfx, seg_start, seg_end)
                else:
                    rows = lxr_fetch(idx["code"], seg_start, seg_end)
                    time.sleep(0.4)
                n = 0
                for rec in rows:
                    d = str(rec.get("date", ""))[:10]
                    if not d:
                        continue
                    for (lk, suf, _nm) in METRICS:
                        n += upsert(conn, f"{pfx}_{suf}", d, rec.get(lk), now)
                conn.commit()
                total += len(rows)
                print(f"   {seg_start.year}: {len(rows)} 天")
                seg_start = datetime.date(seg_start.year + 1, 1, 1)
            for (_lk, suf, _nm) in METRICS:
                conn.execute("INSERT INTO ingest_runs(metric_id,run_at,status,rows_written,message) VALUES(?,?,?,?,?)",
                             (f"{pfx}_{suf}", now, "ok", total, f"from {start_d}"))
            conn.commit()

        print(f"\n[{datetime.datetime.now(CN_TZ):%Y-%m-%d %H:%M} CST] 完成 → {db_path}")
        for idx in INDEXES:
            r = conn.execute("SELECT obs_date,value FROM observations WHERE metric_id=? ORDER BY obs_date DESC LIMIT 1",
                             (f"{idx['id']}_pe",)).fetchone()
            tot = conn.execute("SELECT COUNT(*) FROM observations WHERE metric_id=?", (f"{idx['id']}_pe",)).fetchone()[0]
            if r:
                print(f"  {idx['name']:<7} PE 最新 {r[0]} = {r[1]:.2f}   （PE共{tot}天）")
        return 0
    finally:
        conn.close()


def _sample(pfx, s, e):  # 自测用
    import random
    random.seed(hash(pfx) & 255)
    out = []; d = s
    while d <= e:
        if d.weekday() < 5 and random.random() < 0.9:
            out.append({"date": d.isoformat() + "T00:00:00+08:00",
                        "pe_ttm.mcw": round(random.uniform(10, 40), 2), "pe_ttm.median": round(random.uniform(15, 50), 2),
                        "pb.mcw": round(random.uniform(1, 7), 2), "pb.median": round(random.uniform(2, 8), 2),
                        "ps_ttm.mcw": round(random.uniform(1, 10), 2), "ps_ttm.median": round(random.uniform(2, 13), 2),
                        "dyr.mcw": round(random.uniform(0.001, 0.03), 4), "cp": round(random.uniform(1000, 5000), 2),
                        "ta": 1e11, "fb": 3e11, "mc": 1e13})
        d += datetime.timedelta(days=1)
    return out


def main():
    global _SELFTEST
    if "--selftest" in sys.argv:
        _SELFTEST = True
        db = os.path.join(HERE, "data", "valuation_selftest.db")
        if os.path.exists(db):
            os.remove(db)
        print("[自测] 用模拟数据验证 落库/分段/增量（离线）\n")
        sys.exit(run(db))
    if not TOKEN:
        print("!! 没读到 token：export LIXINGER_TOKEN=你的token"); sys.exit(1)
    print(f"[{datetime.datetime.now(CN_TZ):%Y-%m-%d %H:%M} CST] 开始（首次会分段回填十年，请耐心）…")
    sys.exit(run(DB_PATH))


if __name__ == "__main__":
    main()
