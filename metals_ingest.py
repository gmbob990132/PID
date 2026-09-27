#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
金属模块 · 落库脚本（独立一套，不依赖电解铝/股权代码）

数据源（均已探路验证 GitHub 境外可取）：
  akshare futures_main_sina : 沪金 AU0 / 沪铜 CU0 / 碳酸锂 LC0（收盘价）
  akshare spot_hist_sge     : 上海金 Au99.99（close）
  akshare futures_foreign_hist: COMEX金 GC / LME铜 CAD（外盘 close）
  akshare macro_euro_lme_stock: LME 铜库存（宽表'铜-库存'列，吨→万吨）
  Mysteel li.mysteel.com    : 碳酸锂现货三品级（静态表，取中间价 t9 price；全字段都存）

用法：
  python3 metals_ingest.py            # 联网抓取/增量补齐 → data/metals.db
  python3 metals_ingest.py --selftest # 离线，用内置样本验证解析与落库

结构分层：配置 / 适配器(source) / 落库核心(store) / 主流程(run)
"""
import os
import re
import sys
import sqlite3
from datetime import datetime, date, timedelta, timezone
from urllib.request import Request, urlopen

CN_TZ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "metals.db")
_SELFTEST = False
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

MODULE = {"id": "metals", "name": "金属", "sort_order": 3}

# ---- 指标配置：id, 名称, 单位, 金属分组, source_ref, params, 图上隐藏? ----
METRICS = [
    # 金
    {"id": "au_shfe",  "name": "沪金",          "unit": "元/克",     "group": "gold",   "src": "ak_futures_main",  "params": {"symbol": "AU0"}},
    {"id": "au_sge",   "name": "上海金Au99.99", "unit": "元/克",     "group": "gold",   "src": "ak_spot_sge",      "params": {"symbol": "Au99.99"}},
    {"id": "au_comex", "name": "COMEX金",       "unit": "美元/盎司", "group": "gold",   "src": "ak_foreign",       "params": {"symbol": "GC"}},
    # 铜
    {"id": "cu_shfe",  "name": "沪铜",          "unit": "元/吨",     "group": "copper", "src": "ak_futures_main",  "params": {"symbol": "CU0"}},
    {"id": "cu_lme",   "name": "LME铜",         "unit": "美元/吨",   "group": "copper", "src": "ak_foreign",       "params": {"symbol": "CAD"}},
    {"id": "cu_lme_inv","name": "LME铜库存",    "unit": "万吨",      "group": "copper", "src": "ak_lme_stock",     "params": {"metal": "铜"}, "hide_chart": True},
    # 锂
    {"id": "lc_futures","name": "碳酸锂期货",   "unit": "元/吨",     "group": "lithium","src": "ak_futures_main",  "params": {"symbol": "LC0"}},
]
# 碳酸锂现货三品级：每级存 中间/最低/最高/涨跌 四个指标；名称需与网页 t21.name 精确相等
LC_SPOT = [
    ("lc_spot_prem", "优质电池级", "优质电池级碳酸锂（早盘）"),
    ("lc_spot_batt", "电池级",     "电池级碳酸锂（早盘）"),
    ("lc_spot_ind",  "工业级",     "工业级碳酸锂（早盘）"),
]
for _pid, _short, _full in LC_SPOT:
    METRICS.append({"id": f"{_pid}_mid", "name": f"碳酸锂现货·{_short}", "unit": "元/吨",
                    "group": "lithium", "src": "mysteel_li", "params": {"name": _full, "field": "mid"},
                    "hide_overview": (_pid != "lc_spot_batt")})  # 概览只放电池级；三级走势图都显示
    for _sub, _cn in [("low", "最低"), ("high", "最高"), ("chg", "涨跌")]:
        METRICS.append({"id": f"{_pid}_{_sub}", "name": f"碳酸锂现货·{_short}·{_cn}", "unit": "元/吨",
                        "group": "lithium", "src": "mysteel_li", "params": {"name": _full, "field": _sub},
                        "hide_chart": True, "hide_overview": True})

GROUP_NAME = {"gold": "金", "copper": "铜", "lithium": "锂"}


# ================= 适配器（source 层）=================
def _clean_num(s):
    s = re.sub(r"[^0-9.\-]", "", str(s))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


def ad_ak_futures_main(metrics, start_d):
    import akshare as ak
    start = start_d.strftime("%Y%m%d")
    out = []
    for m in metrics:
        df = ak.futures_main_sina(symbol=m["params"]["symbol"], start_date=start, end_date="22220101")
        col = "收盘价" if "收盘价" in df.columns else next(c for c in df.columns if "close" in str(c).lower())
        dcol = "日期" if "日期" in df.columns else next(c for c in df.columns if "date" in str(c).lower())
        for _, r in df.iterrows():
            v = _clean_num(r[col])
            if v is not None:
                out.append((m["id"], str(r[dcol])[:10], v))
    return out


def ad_ak_spot_sge(metrics, start_d):
    import akshare as ak
    out = []
    for m in metrics:
        df = ak.spot_hist_sge(symbol=m["params"]["symbol"])
        for _, r in df.iterrows():
            v = _clean_num(r.get("close"))
            d = str(r.get("date"))[:10]
            if v is not None and d >= start_d.isoformat():
                out.append((m["id"], d, v))
    return out


def ad_ak_foreign(metrics, start_d):
    import akshare as ak
    out = []
    for m in metrics:
        df = ak.futures_foreign_hist(symbol=m["params"]["symbol"])
        for _, r in df.iterrows():
            v = _clean_num(r.get("close"))
            d = str(r.get("date"))[:10]
            if v is not None and d >= start_d.isoformat():
                out.append((m["id"], d, v))
    return out


def ad_ak_lme_stock(metrics, start_d):
    import akshare as ak
    df = ak.macro_euro_lme_stock()
    cols = list(df.columns)
    dcol = "日期" if "日期" in cols else cols[0]
    out = []
    for m in metrics:
        metal = m["params"]["metal"]
        col = next((c for c in cols if metal in str(c) and "库存" in str(c)), None)
        if col is None:
            raise ValueError(f"LME库存找不到'{metal}-库存'列，列：{cols}")
        for _, r in df.iterrows():
            v = _clean_num(r[col])
            d = str(r[dcol])[:10]
            if v is not None and d >= start_d.isoformat():
                out.append((m["id"], d, round(v / 10000.0, 2)))   # 吨→万吨
    return out


def ad_mysteel_li(metrics, start_d):
    """抓 li.mysteel.com 碳酸锂现货表（静态）。按 name 精确匹配行，取 mid/low/high/chg。
    只有当天一行，日期用表内 t6.time。"""
    if _SELFTEST:
        html = _SAMPLE_LI
    else:
        with urlopen(Request("https://li.mysteel.com/",
                     headers={"User-Agent": _UA, "Referer": "https://li.mysteel.com/"}), timeout=25) as r:
            html = r.read().decode("utf-8", "ignore")
    # 解析所有行：name → {mid,low,high,chg,date}
    rows = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        nm = re.search(r'class="t21 name"[^>]*>(.*?)</td>', tr, re.S)
        if not nm:
            continue
        name = re.sub(r"<[^>]+>", "", nm.group(1)).strip()
        t9 = re.findall(r'<td class="t9"[^>]*>(.*?)</td>', tr, re.S)      # 最低、最高
        price = re.search(r'class="t9 price"[^>]*>(.*?)</td>', tr, re.S)   # 中间价
        rf = re.search(r'data-value-raise1="([\d.]*)"\s+data-value-raise2="([\d.]*)"', tr)
        tm = re.search(r'class="t6 time"[^>]*>(.*?)</td>', tr, re.S)
        low = _clean_num(t9[0]) if len(t9) >= 1 else None
        high = _clean_num(t9[1]) if len(t9) >= 2 else None
        mid = _clean_num(price.group(1)) if price else None
        # 涨跌：raise2 - raise1（raise1=今日中间价, raise2=昨日? 取差；若不可得则 None）
        chg = None
        if rf and rf.group(1) and rf.group(2):
            chg = round(_clean_num(rf.group(1)) - _clean_num(rf.group(2)), 2)
        dd = re.sub(r"<[^>]+>", "", tm.group(1)).strip()[:10] if tm else None
        rows[name] = {"mid": mid, "low": low, "high": high, "chg": chg, "date": dd}

    out = []
    today = datetime.now(CN_TZ).date().isoformat()
    for m in metrics:
        row = rows.get(m["params"]["name"])
        if not row:
            continue
        val = row.get(m["params"]["field"])
        d = row.get("date") or today
        if val is not None:
            out.append((m["id"], d, val))
    return out


ADAPTERS = {
    "ak_futures_main": ad_ak_futures_main,
    "ak_spot_sge": ad_ak_spot_sge,
    "ak_foreign": ad_ak_foreign,
    "ak_lme_stock": ad_ak_lme_stock,
    "mysteel_li": ad_mysteel_li,
}


# ================= 落库核心（store 层）=================
def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS modules(
      id TEXT PRIMARY KEY, name TEXT, sort_order INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS metrics(
      id TEXT PRIMARY KEY, module_id TEXT, name TEXT, grp TEXT, unit TEXT,
      sort_order INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
      hide_overview INTEGER DEFAULT 0, hide_chart INTEGER DEFAULT 0);
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
    for i, m in enumerate(METRICS):
        conn.execute("INSERT INTO metrics(id,module_id,name,grp,unit,sort_order,hide_overview,hide_chart) "
                     "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                     "name=excluded.name, grp=excluded.grp, unit=excluded.unit, "
                     "hide_overview=excluded.hide_overview, hide_chart=excluded.hide_chart",
                     (m["id"], MODULE["id"], m["name"], m["group"], m["unit"], i,
                      1 if m.get("hide_overview") else 0, 1 if m.get("hide_chart") else 0))
    conn.commit()


def latest_date(conn, metric_id):
    r = conn.execute("SELECT MAX(obs_date) FROM observations WHERE metric_id=?", (metric_id,)).fetchone()
    return r[0] if r and r[0] else None


def upsert(conn, metric_id, obs_date, value, source, now_utc):
    conn.execute("INSERT INTO observations(metric_id,obs_date,value,source,ingested_at) "
                 "VALUES(?,?,?,?,?) ON CONFLICT(metric_id,obs_date) DO UPDATE SET "
                 "value=excluded.value, source=excluded.source, ingested_at=excluded.ingested_at",
                 (metric_id, obs_date, value, source, now_utc))


def run(db_path):
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = datetime.now(CN_TZ).date()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        # 按 source 分组
        by_src = {}
        for m in METRICS:
            by_src.setdefault(m["src"], []).append(m)

        all_ok = True
        summary = {}
        for src, ms in by_src.items():
            # 增量起点：该源里最早的"最新日期"往前 7 天重叠；akshare 期货默认拉一年多
            starts = [latest_date(conn, m["id"]) for m in ms]
            if any(s is None for s in starts):
                start_d = (today - timedelta(days=430))   # 首次/有缺失：拉一年多
            else:
                earliest = min(starts)
                start_d = datetime.strptime(earliest, "%Y-%m-%d").date() - timedelta(days=7)
            try:
                rows = ADAPTERS[src](ms, start_d) if not _SELFTEST or src == "mysteel_li" else \
                       ADAPTERS[src](ms, start_d)
                cnt = {}
                for mid, d, v in rows:
                    upsert(conn, mid, d, v, src, now_utc)
                    cnt[mid] = cnt.get(mid, 0) + 1
                for m in ms:
                    n = cnt.get(m["id"], 0)
                    conn.execute("INSERT INTO ingest_runs(metric_id,run_at,status,rows_written,message) "
                                 "VALUES(?,?,?,?,?)", (m["id"], now_utc, "ok" if n else "no_data", n, f"from {start_d}"))
                    summary[m["id"]] = n
            except Exception as ex:
                all_ok = False
                print(f"!! [{src}] 失败：{repr(ex)[:160]}")
                for m in ms:
                    conn.execute("INSERT INTO ingest_runs(metric_id,run_at,status,rows_written,message) "
                                 "VALUES(?,?,?,?,?)", (m["id"], now_utc, "failed", 0, repr(ex)[:180]))
                    summary[m["id"]] = 0
        conn.commit()

        # 反馈：每个分组打印代表指标最新值
        print(f"[{datetime.now(CN_TZ):%Y-%m-%d %H:%M} CST] 完成 → {db_path}")
        for m in METRICS:
            if m.get("hide_overview"):
                continue
            r = conn.execute("SELECT obs_date,value FROM observations WHERE metric_id=? "
                             "ORDER BY obs_date DESC LIMIT 1", (m["id"],)).fetchone()
            tot = conn.execute("SELECT COUNT(*) FROM observations WHERE metric_id=?", (m["id"],)).fetchone()[0]
            if r:
                print(f"  ✓ {m['name']:<16} {r[1]:>12.2f} {m['unit']:<7} 最新 {r[0]}  （共{tot}天）")
            else:
                print(f"  · {m['name']:<16} 无数据")
        return 0 if all_ok else 2
    finally:
        conn.close()


# ---- 自测样本 ----
_SAMPLE_LI = '''
<tr><td class="t21 name">优质电池级碳酸锂（早盘）</td><td class="t12 specs">Li2CO3≥99.6%</td>
<td class="t9">130300</td><td class="t9">132000</td><td class="t9 price">131150</td>
<td class="t9 rise-fall" data-value-raise1="131150" data-value-raise2="133400"></td>
<td class="t8 unit">元/吨</td><td class="t6 time">2026-09-24</td></tr>
<tr><td class="t21 name">电池级碳酸锂（早盘）</td><td class="t12 specs">Li2CO3≥99.5%</td>
<td class="t9">128300</td><td class="t9">132000</td><td class="t9 price">130150</td>
<td class="t9 rise-fall" data-value-raise1="130150" data-value-raise2="132400"></td>
<td class="t8 unit">元/吨</td><td class="t6 time">2026-09-24</td></tr>
<tr><td class="t21 name">工业级碳酸锂（早盘）</td><td class="t12 specs">Li2CO3≥99.2%</td>
<td class="t9">125800</td><td class="t9">129500</td><td class="t9 price">127650</td>
<td class="t9 rise-fall" data-value-raise1="127650" data-value-raise2="129900"></td>
<td class="t8 unit">元/吨</td><td class="t6 time">2026-09-24</td></tr>
'''


def main():
    global _SELFTEST
    if "--selftest" in sys.argv:
        _SELFTEST = True
        db = os.path.join(HERE, "data", "metals_selftest.db")
        if os.path.exists(db):
            os.remove(db)
        print("[自测] 碳酸锂现货用内置样本；akshare 源会因离线失败（正常）\n")
        sys.exit(run(db))
    print(f"[{datetime.now(CN_TZ):%Y-%m-%d %H:%M} CST] 开始 …")
    sys.exit(run(DB_PATH))


if __name__ == "__main__":
    main()
