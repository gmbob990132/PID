#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股权溢价指数模块 · 落库脚本（独立一套，不依赖电解铝代码）

数据源：
  - 中证指数官网接口：沪深300 每日行情 + PE(TTM)（字段 peg 实为 PE_TTM）
  - 中债网：10 年期国债收益率（国债曲线，不会串成票据；替代原 akshare）
衍生：
  - 股权溢价指数 = 1/PE(TTM)*100 − 10Y   （单位：%）

落库策略：
  - 首次（库里无数据）：从 2015-01-01 分批回填（中证单次限 5 年、国债单次限 1 年）
  - 之后：每个指标各查自己库里最新日期，只增量补最新（含少量重叠冗余，幂等 upsert 去重）
  - 幂等：PRIMARY KEY(metric_id, obs_date)

用法：
  python3 erp_ingest.py            # 联网抓取/补齐，写入 data/erp.db
  python3 erp_ingest.py --selftest # 不联网，用内置样本验证 落库+ERP 计算 逻辑
"""
import os
import re
import sys
import json
import sqlite3
from datetime import datetime, date, timedelta, timezone
from urllib.request import Request, urlopen

CN_TZ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "erp.db")
BACKFILL_START = date(2015, 1, 1)
OVERLAP_DAYS = 7
_SELFTEST = False

# ================= 配置：模块与指标 =================
MODULE = {"id": "erp", "name": "股权溢价指数", "sort_order": 2}
CSI_FIELDS = [
    ("close",        "csi300_close",  "沪深300点位", "点",   "price"),
    ("peg",          "csi300_pe_ttm", "市盈率TTM",   "倍",   "valuation"),
    ("open",         "csi300_open",   "开盘",       "点",   "raw"),
    ("high",         "csi300_high",   "最高",       "点",   "raw"),
    ("low",          "csi300_low",    "最低",       "点",   "raw"),
    ("change",       "csi300_change", "涨跌",       "点",   "raw"),
    ("changePct",    "csi300_pct",    "涨跌幅",     "%",    "raw"),
    ("tradingVol",   "csi300_vol",    "成交量",     "手",   "raw"),
    ("tradingValue", "csi300_amount", "成交额",     "元",   "raw"),
]
BOND_METRIC = ("cn_10y_yield", "10年国债收益率", "%", "rate")
ERP_METRIC = ("csi300_erp", "股权溢价指数", "%", "valuation")


def all_metrics():
    ms = [{"id": mid, "name": nm, "unit": u, "category": cat}
          for (_f, mid, nm, u, cat) in CSI_FIELDS]
    ms.append({"id": BOND_METRIC[0], "name": BOND_METRIC[1], "unit": BOND_METRIC[2], "category": BOND_METRIC[3]})
    ms.append({"id": ERP_METRIC[0], "name": ERP_METRIC[1], "unit": ERP_METRIC[2], "category": ERP_METRIC[3]})
    return ms

# ================= 适配器（source 层）=================
_CSI_URL = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
            "?indexCode=000300&startDate={s}&endDate={e}")
_CSI_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.csindex.com.cn/", "Accept": "application/json, text/plain, */*"}
_BOND_URL = ("https://yield.chinabond.com.cn/cbweb-cbrc-web/cbrc/historyQuery"
             "?startDate={s}&endDate={e}&gjqx=10&qxId=hzsylqx&locale=en_US&mark=1")
_BOND_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                 "Referer": "https://yield.chinabond.com.cn/", "Accept": "text/html,*/*"}


def _norm_date(v):
    s = str(v)
    if 'T' in s:
        s = s.split('T')[0]
    s = s.replace('/', '-')
    if len(s) == 8 and s.isdigit():
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]


def _find_list(obj):
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and 'tradeDate' in obj[0]:
            return obj
        for x in obj:
            r = _find_list(x)
            if r:
                return r
    elif isinstance(obj, dict):
        for v in obj.values():
            r = _find_list(v)
            if r:
                return r
    return None


def _to_float(v):
    try:
        if v in (None, "", "-"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def csi_fetch_window(start_ymd, end_ymd):
    req = Request(_CSI_URL.format(s=start_ymd, e=end_ymd), headers=_CSI_HEADERS)
    with urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode('utf-8'))
    rows = _find_list(data)
    if rows is None:
        raise ValueError("中证返回里找不到 tradeDate 列表；前300字：" + json.dumps(data, ensure_ascii=False)[:300])
    out = []
    for r in rows:
        rec = {"obs_date": _norm_date(r.get("tradeDate"))}
        for (raw_f, mid, _nm, _u, _cat) in CSI_FIELDS:
            rec[mid] = _to_float(r.get(raw_f))
        out.append(rec)
    return out


def csi_fetch_range(start_d, end_d):
    all_rows = []
    seg_start = start_d
    while seg_start <= end_d:
        seg_end = min(date(seg_start.year + 4, 12, 31), end_d)
        all_rows += csi_fetch_window(seg_start.strftime("%Y%m%d"), seg_end.strftime("%Y%m%d"))
        seg_start = seg_end + timedelta(days=1)
    return all_rows


def bond_fetch_range(start_d, end_d):
    """从中债网取 10 年期国债收益率（国债曲线，不会串成票据）。
    接口一次限一年，按年分段。返回 {日期str: 10Y收益率}。"""
    y = {}
    seg_start = start_d
    while seg_start <= end_d:
        seg_end = min(date(seg_start.year, 12, 31), end_d)
        req = Request(_BOND_URL.format(s=seg_start.strftime("%Y-%m-%d"), e=seg_end.strftime("%Y-%m-%d")),
                      headers=_BOND_HEADERS)
        with urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", "ignore")
        for tr in re.findall(r"<tr>(.*?)</tr>", html, re.S):
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            d = next((c for c in cells if re.match(r"\d{4}-\d{2}-\d{2}$", c)), None)
            vals = [c for c in cells if re.match(r"\d+\.\d+$", c)]
            if d and vals:
                y[d] = float(vals[0])
        seg_start = date(seg_start.year + 1, 1, 1)
    return y

# ================= 落库核心（store 层）=================
def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS modules(
      id TEXT PRIMARY KEY, name TEXT NOT NULL, sort_order INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS metrics(
      id TEXT PRIMARY KEY, module_id TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
      unit TEXT, sort_order INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS observations(
      metric_id TEXT NOT NULL, obs_date TEXT NOT NULL, value REAL NOT NULL,
      source TEXT, ingested_at TEXT, PRIMARY KEY(metric_id, obs_date));
    CREATE TABLE IF NOT EXISTS ingest_runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, metric_id TEXT, run_at TEXT,
      status TEXT, rows_written INTEGER DEFAULT 0, message TEXT);
    """)
    conn.execute("INSERT INTO modules(id,name,sort_order) VALUES(?,?,?) "
                 "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                 (MODULE["id"], MODULE["name"], MODULE["sort_order"]))
    for i, m in enumerate(all_metrics()):
        conn.execute("INSERT INTO metrics(id,module_id,name,category,unit,sort_order) "
                     "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                     "name=excluded.name, unit=excluded.unit, category=excluded.category",
                     (m["id"], MODULE["id"], m["name"], m["category"], m["unit"], i))
    conn.commit()


def latest_date(conn, metric_id):
    r = conn.execute("SELECT MAX(obs_date) FROM observations WHERE metric_id=?", (metric_id,)).fetchone()
    return r[0] if r and r[0] else None


def upsert(conn, metric_id, obs_date, value, source, now_utc):
    if value is None:
        return 0
    conn.execute("INSERT INTO observations(metric_id,obs_date,value,source,ingested_at) "
                 "VALUES(?,?,?,?,?) ON CONFLICT(metric_id,obs_date) DO UPDATE SET "
                 "value=excluded.value, source=excluded.source, ingested_at=excluded.ingested_at",
                 (metric_id, obs_date, value, source, now_utc))
    return 1


def log_run(conn, metric_id, status, rows, msg, now_utc):
    conn.execute("INSERT INTO ingest_runs(metric_id,run_at,status,rows_written,message) "
                 "VALUES(?,?,?,?,?)", (metric_id, now_utc, status, rows, msg))


def start_from(conn, metric_id, today):
    last = latest_date(conn, metric_id)
    if last:
        d = datetime.strptime(last, "%Y-%m-%d").date() - timedelta(days=OVERLAP_DAYS)
        return max(d, BACKFILL_START)
    return BACKFILL_START

# ================= 衍生（derive 层）=================
def compute_erp(conn, now_utc):
    rows = conn.execute(
        "SELECT a.obs_date, a.value AS pe, b.value AS y10 FROM observations a "
        "JOIN observations b ON a.obs_date=b.obs_date "
        "WHERE a.metric_id='csi300_pe_ttm' AND b.metric_id='cn_10y_yield' AND a.value<>0",
    ).fetchall()
    n = 0
    for obs_date, pe, y10 in rows:
        erp = 100.0 / pe - y10
        n += upsert(conn, ERP_METRIC[0], obs_date, round(erp, 4), "derived", now_utc)
    return n

# ================= 主流程（run 层）=================
def run(db_path):
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = datetime.now(CN_TZ).date()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)

        csi_start = start_from(conn, "csi300_close", today)
        first_backfill = (latest_date(conn, "csi300_close") is None)
        try:
            if _SELFTEST:
                csi_rows = _SAMPLE_CSI
            else:
                csi_rows = csi_fetch_range(csi_start, today)
            written = {mid: 0 for (_f, mid, _n, _u, _c) in CSI_FIELDS}
            for rec in csi_rows:
                for (_f, mid, _n, _u, _c) in CSI_FIELDS:
                    written[mid] += upsert(conn, mid, rec["obs_date"], rec.get(mid), "csindex", now_utc)
            for (_f, mid, _n, _u, _c) in CSI_FIELDS:
                log_run(conn, mid, "ok", written[mid], f"from {csi_start}", now_utc)
            csi_ok = True
        except Exception as ex:
            for (_f, mid, _n, _u, _c) in CSI_FIELDS:
                log_run(conn, mid, "failed", 0, str(ex)[:200], now_utc)
            print("!! 中证取数失败：", ex)
            csi_ok = False

        bond_start = start_from(conn, BOND_METRIC[0], today)
        try:
            if _SELFTEST:
                y10 = _SAMPLE_BOND
            else:
                y10 = bond_fetch_range(bond_start, today)
            nb = 0
            for d, v in y10.items():
                nb += upsert(conn, BOND_METRIC[0], d, v, "chinabond", now_utc)
            log_run(conn, BOND_METRIC[0], "ok", nb, f"from {bond_start}", now_utc)
            bond_ok = True
        except Exception as ex:
            log_run(conn, BOND_METRIC[0], "failed", 0, str(ex)[:200], now_utc)
            print("!! 国债取数失败：", ex)
            bond_ok = False

        n_erp = compute_erp(conn, now_utc)
        log_run(conn, ERP_METRIC[0], "ok", n_erp, "computed", now_utc)
        conn.commit()

        mode = "首次回填" if first_backfill else "增量补齐"
        print(f"[{datetime.now(CN_TZ):%Y-%m-%d %H:%M} CST] {mode}完成 → {db_path}")
        for mid in ("csi300_close", "csi300_pe_ttm", "cn_10y_yield", "csi300_erp"):
            row = conn.execute("SELECT obs_date,value FROM observations WHERE metric_id=? "
                               "ORDER BY obs_date DESC LIMIT 1", (mid,)).fetchone()
            total = conn.execute("SELECT COUNT(*) FROM observations WHERE metric_id=?", (mid,)).fetchone()[0]
            nm = {"csi300_close": "沪深300点位", "csi300_pe_ttm": "PE(TTM)",
                  "cn_10y_yield": "10年国债", "csi300_erp": "股权溢价指数"}[mid]
            if row:
                val = f"{row[1]:.2f}" if mid != "csi300_erp" else f"{row[1]:.2f}%"
                print(f"  {nm:<10} 最新 {row[0]} = {val}   （库内共 {total} 天）")
            else:
                print(f"  {nm:<10} 无数据")
        return 0 if (csi_ok and bond_ok) else 2
    finally:
        conn.close()

# ---- 自测样本 ----
_SAMPLE_CSI = [
    {"obs_date": "2026-09-03", "csi300_close": 4552.58, "csi300_pe_ttm": 13.65,
     "csi300_open": 4540.0, "csi300_high": 4560.0, "csi300_low": 4530.0,
     "csi300_change": 4.6, "csi300_pct": 0.1, "csi300_vol": 1e8, "csi300_amount": 2e11},
    {"obs_date": "2026-09-11", "csi300_close": 4530.0, "csi300_pe_ttm": 13.52,
     "csi300_open": 4520.0, "csi300_high": 4540.0, "csi300_low": 4510.0,
     "csi300_change": -2.0, "csi300_pct": -0.04, "csi300_vol": 1e8, "csi300_amount": 2e11},
]
_SAMPLE_BOND = {"2026-09-03": 1.6798, "2026-09-11": 1.6899}


def main():
    global _SELFTEST
    if "--selftest" in sys.argv:
        _SELFTEST = True
        db = os.path.join(HERE, "data", "erp_selftest.db")
        if os.path.exists(db):
            os.remove(db)
        print("[自测] 用内置样本验证 落库 + ERP 计算（离线）\n")
        sys.exit(run(db))
    print(f"[{datetime.now(CN_TZ):%Y-%m-%d %H:%M} CST] 开始 …")
    sys.exit(run(DB_PATH))


if __name__ == "__main__":
    main()
