#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股权溢价指数模块 · 落库脚本（多指数：沪深300 + 科创50）

数据源：
  - 中证指数官网接口：各指数每日行情 + PE(TTM)（字段 peg 实为 PE_TTM），按 indexCode 区分
  - 中债网：10 年期国债收益率（共用）
衍生：
  - {指数}股权溢价指数 = 1/PE(TTM)*100 − 10Y   （单位：%）

用法：
  python3 erp_ingest.py            # 联网抓取/补齐，写入 data/erp.db
  python3 erp_ingest.py --selftest # 不联网，用内置样本验证 落库+ERP 计算
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
OVERLAP_DAYS = 7
_SELFTEST = False

# ================= 配置：指数列表 + 字段模板 =================
MODULE = {"id": "erp", "name": "股权溢价指数", "sort_order": 2}
# 每个指数：id(指标前缀) / 中证代码 / 中文名 / 回填起点
INDEXES = [
    {"id": "csi300", "code": "000300", "name": "沪深300", "start": date(2015, 1, 1)},
    {"id": "star50", "code": "000688", "name": "科创50", "start": date(2019, 12, 31)},
]
# 中证返回字段 → (原始字段, 指标后缀, 名称尾, 单位, 类别)
CSI_FIELDS = [
    ("close",        "close",  "点位",     "点", "price"),
    ("peg",          "pe_ttm", "市盈率TTM", "倍", "valuation"),
    ("open",         "open",   "开盘",     "点", "raw"),
    ("high",         "high",   "最高",     "点", "raw"),
    ("low",          "low",    "最低",     "点", "raw"),
    ("change",       "change", "涨跌",     "点", "raw"),
    ("changePct",    "pct",    "涨跌幅",   "%",  "raw"),
    ("tradingVol",   "vol",    "成交量",   "手", "raw"),
    ("tradingValue", "amount", "成交额",   "元", "raw"),
]
BOND_METRIC = ("cn_10y_yield", "10年国债收益率", "%", "rate")


def all_metrics():
    ms = []
    for idx in INDEXES:
        for (_f, suf, tail, u, cat) in CSI_FIELDS:
            ms.append({"id": f"{idx['id']}_{suf}", "name": f"{idx['name']}{tail}", "unit": u, "category": cat})
        ms.append({"id": f"{idx['id']}_erp", "name": f"{idx['name']}股权溢价指数", "unit": "%", "category": "valuation"})
    ms.append({"id": BOND_METRIC[0], "name": BOND_METRIC[1], "unit": BOND_METRIC[2], "category": BOND_METRIC[3]})
    return ms

# ================= 适配器（source 层）=================
_CSI_URL = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
            "?indexCode={code}&startDate={s}&endDate={e}")
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


def csi_fetch_window(code, prefix, start_ymd, end_ymd):
    req = Request(_CSI_URL.format(code=code, s=start_ymd, e=end_ymd), headers=_CSI_HEADERS)
    with urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode('utf-8'))
    rows = _find_list(data)
    if rows is None:
        raise ValueError("中证返回里找不到 tradeDate 列表；前300字：" + json.dumps(data, ensure_ascii=False)[:300])
    out = []
    for r in rows:
        rec = {"obs_date": _norm_date(r.get("tradeDate"))}
        for (raw_f, suf, _t, _u, _c) in CSI_FIELDS:
            rec[f"{prefix}_{suf}"] = _to_float(r.get(raw_f))
        out.append(rec)
    return out


def csi_fetch_range(code, prefix, start_d, end_d):
    all_rows = []
    seg_start = start_d
    while seg_start <= end_d:
        seg_end = min(date(seg_start.year + 4, 12, 31), end_d)
        all_rows += csi_fetch_window(code, prefix, seg_start.strftime("%Y%m%d"), seg_end.strftime("%Y%m%d"))
        seg_start = seg_end + timedelta(days=1)
    return all_rows


def bond_fetch_range(start_d, end_d):
    """从中债网取 10 年期国债收益率。接口一次限一年，按年分段。返回 {日期str: 10Y}。"""
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


def start_from(conn, metric_id, backfill_start, today):
    last = latest_date(conn, metric_id)
    if last:
        d = datetime.strptime(last, "%Y-%m-%d").date() - timedelta(days=OVERLAP_DAYS)
        return max(d, backfill_start)
    return backfill_start

# ================= 衍生（derive 层）=================
def compute_erp(conn, prefix, now_utc):
    rows = conn.execute(
        "SELECT a.obs_date, a.value AS pe, b.value AS y10 FROM observations a "
        "JOIN observations b ON a.obs_date=b.obs_date "
        "WHERE a.metric_id=? AND b.metric_id='cn_10y_yield' AND a.value<>0",
        (f"{prefix}_pe_ttm",)).fetchall()
    n = 0
    for obs_date, pe, y10 in rows:
        n += upsert(conn, f"{prefix}_erp", obs_date, round(100.0 / pe - y10, 4), "derived", now_utc)
    return n

# ================= 主流程（run 层）=================
def run(db_path):
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = datetime.now(CN_TZ).date()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        min_start = min(idx["start"] for idx in INDEXES)
        first_backfill = (latest_date(conn, f"{INDEXES[0]['id']}_close") is None)
        all_ok = True

        # ---- 国债（共用，取所有指数需要的最早起点）----
        bond_start = start_from(conn, BOND_METRIC[0], min_start, today)
        try:
            y10 = _SAMPLE_BOND if _SELFTEST else bond_fetch_range(bond_start, today)
            nb = 0
            for d, v in y10.items():
                nb += upsert(conn, BOND_METRIC[0], d, v, "chinabond", now_utc)
            log_run(conn, BOND_METRIC[0], "ok", nb, f"from {bond_start}", now_utc)
        except Exception as ex:
            all_ok = False
            log_run(conn, BOND_METRIC[0], "failed", 0, str(ex)[:200], now_utc)
            print("!! 国债取数失败：", ex)

        # ---- 各指数：取行情+PE，算 ERP ----
        for idx in INDEXES:
            pfx = idx["id"]
            csi_start = start_from(conn, f"{pfx}_close", idx["start"], today)
            try:
                if _SELFTEST:
                    csi_rows = _SAMPLE_CSI.get(pfx, [])
                else:
                    csi_rows = csi_fetch_range(idx["code"], pfx, csi_start, today)
                written = {f"{pfx}_{suf}": 0 for (_f, suf, _t, _u, _c) in CSI_FIELDS}
                for rec in csi_rows:
                    for (_f, suf, _t, _u, _c) in CSI_FIELDS:
                        mid = f"{pfx}_{suf}"
                        written[mid] += upsert(conn, mid, rec["obs_date"], rec.get(mid), "csindex", now_utc)
                for mid, n in written.items():
                    log_run(conn, mid, "ok", n, f"from {csi_start}", now_utc)
            except Exception as ex:
                all_ok = False
                for (_f, suf, _t, _u, _c) in CSI_FIELDS:
                    log_run(conn, f"{pfx}_{suf}", "failed", 0, str(ex)[:200], now_utc)
                print(f"!! [{idx['name']}] 中证取数失败：", ex)
            n_erp = compute_erp(conn, pfx, now_utc)
            log_run(conn, f"{pfx}_erp", "ok", n_erp, "computed", now_utc)
        conn.commit()

        # ---- 反馈 ----
        mode = "首次回填" if first_backfill else "增量补齐"
        print(f"[{datetime.now(CN_TZ):%Y-%m-%d %H:%M} CST] {mode}完成 → {db_path}")
        b = conn.execute("SELECT obs_date,value FROM observations WHERE metric_id='cn_10y_yield' "
                         "ORDER BY obs_date DESC LIMIT 1").fetchone()
        bt = conn.execute("SELECT COUNT(*) FROM observations WHERE metric_id='cn_10y_yield'").fetchone()[0]
        if b:
            print(f"  10年国债      最新 {b[0]} = {b[1]:.4f}   （共 {bt} 天）")
        for idx in INDEXES:
            pfx = idx["id"]
            print(f"  —— {idx['name']} ——")
            for suf, lab, pct in [("close", "点位", False), ("pe_ttm", "PE(TTM)", False), ("erp", "股权溢价指数", True)]:
                row = conn.execute("SELECT obs_date,value FROM observations WHERE metric_id=? "
                                   "ORDER BY obs_date DESC LIMIT 1", (f"{pfx}_{suf}",)).fetchone()
                tot = conn.execute("SELECT COUNT(*) FROM observations WHERE metric_id=?", (f"{pfx}_{suf}",)).fetchone()[0]
                if row:
                    v = f"{row[1]:.2f}%" if pct else f"{row[1]:.2f}"
                    print(f"     {lab:<10} 最新 {row[0]} = {v}   （共 {tot} 天）")
                else:
                    print(f"     {lab:<10} 无数据")
        return 0 if all_ok else 2
    finally:
        conn.close()

# ---- 自测样本 ----
_SAMPLE_CSI = {
    "csi300": [
        {"obs_date": "2026-09-11", "csi300_close": 4510.16, "csi300_pe_ttm": 13.52,
         "csi300_open": 4514.3, "csi300_high": 4520.17, "csi300_low": 4461.57,
         "csi300_change": -38.23, "csi300_pct": -0.84, "csi300_vol": 2.04e10, "csi300_amount": 5222.07},
    ],
    "star50": [
        {"obs_date": "2026-09-11", "star50_close": 1553.39, "star50_pe_ttm": 69.23,
         "star50_open": 1548.7, "star50_high": 1556.68, "star50_low": 1516.2,
         "star50_change": -15.82, "star50_pct": -1.01, "star50_vol": 9.97e8, "star50_amount": 778.8},
    ],
}
_SAMPLE_BOND = {"2026-09-11": 1.6899}


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
