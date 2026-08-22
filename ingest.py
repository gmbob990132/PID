#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
落库脚本（B-1：多数据源，适配器注册表 + 调度循环）
抓取各来源 → 写 SQLite → 记抓取日志。

用法：
    python3 ingest.py             # 联网抓取，写入 data/portfolio.db
    python3 ingest.py --selftest  # 不联网，用内置 Mysteel 样本验证（akshare 源跳过）

加新数据源 = 写一个 adapter 注册进 ADAPTERS；加指标 = 在 METRICS 加一行。
Mysteel 路径零依赖；akshare 路径懒加载（仅在有 akshare 指标时才 import）。
"""
import os
import re
import sys
import json
import sqlite3
from datetime import datetime, timezone, timedelta, date
from urllib.request import Request, urlopen

CN_TZ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "portfolio.db")
_SELFTEST = False

MODULE = {"id": "aluminum", "name": "电解铝", "sort_order": 1}
METRICS = [
    {"id": "al_spot_east",    "name": "现货·华东", "category": "price",     "unit": "元/吨",
     "source_type": "api",  "source_ref": "mysteel_flash", "update_freq": "daily"},
    {"id": "al_spot_south",   "name": "现货·华南", "category": "price",     "unit": "元/吨",
     "source_type": "api",  "source_ref": "mysteel_flash", "update_freq": "daily"},
    {"id": "al_spot_central", "name": "现货·中原", "category": "price",     "unit": "元/吨",
     "source_type": "api",  "source_ref": "mysteel_flash", "update_freq": "daily"},
    {"id": "al_social_inv",   "name": "社会库存",  "category": "inventory", "unit": "万吨",
     "source_type": "api",  "source_ref": "mysteel_flash", "update_freq": "biweekly"},
    {"id": "al_futures_main", "name": "沪铝主力", "category": "price", "unit": "元/吨",
     "source_type": "free", "source_ref": "akshare_futures_main", "update_freq": "daily",
     "params": {"symbol": "AL0"}},
    {"id": "al_alumina_futures", "name": "氧化铝主力", "category": "cost", "unit": "元/吨",
     "source_type": "free", "source_ref": "akshare_futures_main", "update_freq": "daily",
     "params": {"symbol": "AO0"}},
    {"id": "al_lme_price", "name": "LME铝价", "category": "price", "unit": "美元/吨",
     "source_type": "free", "source_ref": "akshare_foreign_hist", "update_freq": "daily",
     "params": {"symbol": "AHD"}},
    {"id": "al_lme_inv", "name": "LME库存", "category": "inventory", "unit": "万吨",
     "source_type": "free", "source_ref": "akshare_lme_stock", "update_freq": "daily",
     "params": {"metal": "铝"}},
    {"id": "al_alumina_spot", "name": "氧化铝现货", "category": "cost", "unit": "元/吨",
     "source_type": "free", "source_ref": "akshare_spot_price_daily", "update_freq": "daily",
     "params": {"symbol": "AO"}},
    {"id": "al_anode", "name": "预焙阳极", "category": "cost", "unit": "元/吨",
     "source_type": "manual", "source_ref": "manual_csv", "update_freq": "weekly"},
    {"id": "al_spot_premium", "name": "现货升贴水", "category": "price", "unit": "元/吨",
     "source_type": "derived", "source_ref": "derived", "update_freq": "daily",
     "params": {"formula": "spot_minus_futures", "minuend": "al_spot_east", "subtrahend": "al_futures_main"}},
]

_MY_URL = ("https://openapi.mysteel.com/without_sign/newsflash/flashnews/query_by_tags.htm"
           "?advertisementFlag=0&keyword=&pageNo=1&pageSize=30&sortByScore=false"
           "&columnIds=%255B%255B2%252C84%252C584%255D%255D&breedTagId=4437")
_MY_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
               "Referer": "https://www.mysteel.com/fastcomment/",
               "Accept": "application/json, text/plain, */*"}
_REGION_RE = re.compile(r'(华东|华南|华北|中原|西南|西北|东北)\s*([\d]+(?:\.\d+)?)\s*(?:元/吨)?\s*[，,]\s*'
                        r'(涨|跌|平|持平|稳)\s*([\d]+(?:\.\d+)?)?')
_INV_TOTAL_RE = re.compile(r'库存总量为\s*([\d.]+)\s*万吨')
_INV_CHANGE_RE = re.compile(r'较上期数据\s*(增加|减少|下降|持平)?\s*([\d.]+)?\s*万吨')
_REGION_TO_METRIC = {"华东": "al_spot_east", "华南": "al_spot_south", "中原": "al_spot_central"}

_MY_SAMPLE = {"data": {"list": [
    {"content": "8月21日Mysteel铝锭价格行情:华东23680,涨80;华南23850,涨140;中原23620,涨90(单位:元/吨)",
     "publisherTime": 1787279319755},
    {"content": "Mysteel库存速递：中国铝锭现货库存总量为84.9万吨，较上期数据减少1.4万吨。",
     "publisherTime": 1787187503324},
    {"content": "8月20日Mysteel铝锭价格行情：华东23600，跌70；华南23710，跌80；中原23530，跌60（单位：元/吨）",
     "publisherTime": 1787192923000},
    {"content": "8月19日Mysteel铝锭价格行情：华东23670，跌230；华南23790，跌240；中原23590，跌210（单位：元/吨）",
     "publisherTime": 1787106523000},
]}}


def _find_items(obj):
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and 'content' in obj[0]:
            return obj
        for x in obj:
            r = _find_items(x)
            if r:
                return r
    elif isinstance(obj, dict):
        for v in obj.values():
            r = _find_items(v)
            if r:
                return r
    return None


def _parse_flash(content, obs_date):
    out = []
    if '铝锭价格行情' in content:
        for region, price, direction, amt in _REGION_RE.findall(content):
            mid = _REGION_TO_METRIC.get(region)
            if not mid:
                continue
            chg = float(amt) if amt else 0.0
            if direction == '跌':
                chg = -chg
            elif direction in ('平', '持平', '稳'):
                chg = 0.0
            out.append((mid, float(price), chg))
    if '库存总量为' in content and '铝锭' in content:
        mt = _INV_TOTAL_RE.search(content)
        if mt:
            chg = 0.0
            mc = _INV_CHANGE_RE.search(content)
            if mc and mc.group(2):
                chg = float(mc.group(2))
                if mc.group(1) in ('减少', '下降'):
                    chg = -chg
                elif mc.group(1) == '持平':
                    chg = 0.0
            out.append(("al_social_inv", float(mt.group(1)), chg))
    return out


def adapter_mysteel_flash(metrics):
    if _SELFTEST:
        raw = _MY_SAMPLE
    else:
        raw = None
        last_err = None
        for _attempt in range(3):
            try:
                req = Request(_MY_URL, headers=_MY_HEADERS)
                with urlopen(req, timeout=15) as resp:
                    raw = json.loads(resp.read().decode('utf-8'))
                break
            except Exception as e:
                last_err = e
                import time as _t
                _t.sleep(3)
        if raw is None:
            raise last_err
    items = _find_items(raw)
    if items is None:
        raise ValueError("Mysteel 返回里找不到 content 列表，接口结构可能变了")
    obs = []
    for it in items:
        content = it.get('content', '')
        ms = it.get('publisherTime')
        d = datetime.fromtimestamp(ms / 1000, tz=CN_TZ).date().isoformat() if ms \
            else datetime.now(CN_TZ).date().isoformat()
        for metric_id, value, src_change in _parse_flash(content, d):
            obs.append({"metric_id": metric_id, "obs_date": d, "value": value,
                        "src_change": src_change, "source": "mysteel_openapi"})
    return obs


def _price_rows_to_obs(df, metric, source_prefix):
    import pandas as pd
    cols = list(df.columns)
    dcol = "日期" if "日期" in cols else next((c for c in cols if "date" in str(c).lower() or "日期" in str(c)), cols[0])
    ccol = "收盘价" if "收盘价" in cols else next((c for c in cols if "close" in str(c).lower() or "收盘" in str(c)), None)
    if ccol is None:
        raise ValueError("找不到收盘价/close 列，列为：" + str(cols))
    sym = metric.get("params", {}).get("symbol", "")
    out = []
    for _, r in df.iterrows():
        v = r[ccol]
        if pd.isna(v):
            continue
        out.append({"metric_id": metric["id"], "obs_date": str(pd.to_datetime(r[dcol]).date()),
                    "value": float(v), "src_change": None, "source": source_prefix + sym})
    return out


def adapter_akshare_futures_main(metrics):
    if _SELFTEST:
        return []
    import akshare as ak
    start = (date.today() - timedelta(days=400)).strftime("%Y%m%d")
    out = []
    for m in metrics:
        df = ak.futures_main_sina(symbol=m["params"]["symbol"], start_date=start, end_date="22220101")
        out += _price_rows_to_obs(df, m, "akshare/futures_main_sina/")
    return out


def adapter_akshare_foreign_hist(metrics):
    """外盘期货历史（LME 等），如 LME 铝 symbol=AHD。列名 date/close。"""
    if _SELFTEST:
        return []
    import akshare as ak
    out = []
    for m in metrics:
        df = ak.futures_foreign_hist(symbol=m["params"]["symbol"])
        out += _price_rows_to_obs(df, m, "akshare/futures_foreign_hist/")
    return out


def adapter_akshare_lme_stock(metrics):
    """LME 库存（宽表，按金属名取列，吨→万吨）。"""
    if _SELFTEST:
        return []
    import akshare as ak
    import pandas as pd
    df = ak.macro_euro_lme_stock()
    cols = list(df.columns)
    dcol = "日期" if "日期" in cols else next((c for c in cols if "date" in str(c).lower() or "日期" in str(c)), cols[0])
    out = []
    for m in metrics:
        metal = m.get("params", {}).get("metal", "铝")
        cand = [c for c in cols if metal in str(c)]
        if not cand:
            raise ValueError("LME库存找不到['" + metal + "']列，现有列：" + str(cols))
        col = next((c for c in cand if "库存" in str(c) or "stock" in str(c).lower()), cand[0])
        for _, r in df.iterrows():
            v = r[col]
            if pd.isna(v):
                continue
            out.append({"metric_id": m["id"], "obs_date": str(pd.to_datetime(r[dcol]).date()),
                        "value": round(float(v) / 10000.0, 2), "src_change": None,
                        "source": "akshare/macro_euro_lme_stock/" + metal})
    return out


def adapter_akshare_spot_sys(metrics):
    """生意社现期图-市场价格（如氧化铝现货）。symbol 需匹配生意社品种名，自动适配。"""
    if _SELFTEST:
        return []
    import akshare as ak
    import pandas as pd
    try:
        name_dict = ak.futures_spot_sys.__globals__['__get_sys_spot_futures_dict']()
    except Exception:
        name_dict = {}
    out = []
    for m in metrics:
        want = m["params"]["symbol"]
        sym = want
        if name_dict and want not in name_dict:
            cand = [k for k in name_dict if "氧化铝" in str(k)] if want == "氧化铝" \
                else [k for k in name_dict if want in str(k)]
            if cand:
                sym = cand[0]
            else:
                al = [k for k in name_dict if "铝" in str(k)]
                raise ValueError("生意社无品种'" + want + "'，含铝的可选：" + str(al))
        df = ak.futures_spot_sys(symbol=sym, indicator="市场价格")
        cols = list(df.columns)
        dcol = "日期" if "日期" in cols else cols[0]
        vcol = "现货价格" if "现货价格" in cols else next((c for c in cols if c != dcol), None)
        if vcol is None:
            raise ValueError("生意社现货找不到价格列，列为：" + str(cols))
        for _, r in df.iterrows():
            v = r[vcol]
            if pd.isna(v):
                continue
            out.append({"metric_id": m["id"], "obs_date": str(pd.to_datetime(r[dcol]).date()),
                        "value": float(v), "src_change": None,
                        "source": "akshare/futures_spot_sys/" + sym})
    return out


def adapter_manual_csv(metrics):
    """手动录入：读 manual_prices.csv（列 metric_id,date,value）。低频数据手填。"""
    import csv
    path = os.path.join(HERE, "manual_prices.csv")
    if not os.path.exists(path):
        return []
    wanted = {m["id"] for m in metrics}
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            mid = (row.get("metric_id") or "").strip()
            d = (row.get("date") or "").strip()
            v = (row.get("value") or "").strip()
            if mid in wanted and d and v:
                out.append({"metric_id": mid, "obs_date": d, "value": float(v),
                            "src_change": None, "source": "manual"})
    return out


def adapter_akshare_spot_price_daily(metrics):
    """生意社大宗现货价（按代码，如氧化铝 AO）。每次取最近约10天，靠日积累成历史。"""
    if _SELFTEST:
        return []
    import akshare as ak
    import pandas as pd
    from datetime import date as _date, timedelta as _td
    end = _date.today()
    start = end - _td(days=30)
    out = []
    for m in metrics:
        code = m["params"]["symbol"]
        df = ak.futures_spot_price_daily(start_day=start.strftime("%Y%m%d"),
                                         end_day=end.strftime("%Y%m%d"), vars_list=[code])
        if df is None or len(df) == 0:
            continue
        for _, r in df.iterrows():
            var = str(r.get("var", ""))
            if code not in var and "氧化铝" not in var:
                continue
            v = r.get("sp")
            if pd.isna(v) or float(v) == 0:
                continue
            out.append({"metric_id": m["id"], "obs_date": str(pd.to_datetime(str(r["date"])).date()),
                        "value": float(v), "src_change": None,
                        "source": "akshare/futures_spot_price_daily/" + code})
    return out


ADAPTERS = {
    "mysteel_flash": adapter_mysteel_flash,
    "akshare_futures_main": adapter_akshare_futures_main,
    "akshare_foreign_hist": adapter_akshare_foreign_hist,
    "akshare_lme_stock": adapter_akshare_lme_stock,
    "akshare_spot_sys": adapter_akshare_spot_sys,
    "akshare_spot_price_daily": adapter_akshare_spot_price_daily,
    "manual_csv": adapter_manual_csv,
}


def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS modules(
      id TEXT PRIMARY KEY, name TEXT NOT NULL, sort_order INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS metrics(
      id TEXT PRIMARY KEY, module_id TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
      unit TEXT, source_type TEXT, source_ref TEXT, update_freq TEXT,
      sort_order INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS observations(
      metric_id TEXT NOT NULL, obs_date TEXT NOT NULL, value REAL NOT NULL,
      src_change REAL, source TEXT, status TEXT DEFAULT 'ok', ingested_at TEXT,
      PRIMARY KEY(metric_id, obs_date));
    CREATE TABLE IF NOT EXISTS ingest_runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, metric_id TEXT,
      run_at TEXT, status TEXT, rows_written INTEGER DEFAULT 0, message TEXT);
    """)
    conn.execute("INSERT INTO modules(id,name,sort_order) VALUES(?,?,?) "
                 "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                 (MODULE["id"], MODULE["name"], MODULE["sort_order"]))
    for i, m in enumerate(METRICS):
        conn.execute(
            "INSERT INTO metrics(id,module_id,name,category,unit,source_type,source_ref,update_freq,sort_order) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "name=excluded.name, unit=excluded.unit, category=excluded.category, "
            "source_type=excluded.source_type, source_ref=excluded.source_ref",
            (m["id"], MODULE["id"], m["name"], m["category"], m["unit"],
             m["source_type"], m["source_ref"], m["update_freq"], i))
    conn.commit()


def run(db_path):
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        groups = {}
        for m in METRICS:
            if m.get("active", 1) and m["source_ref"] != "derived":
                groups.setdefault(m["source_ref"], []).append(m)

        all_by_metric = {}
        for source_ref, ms in groups.items():
            adapter = ADAPTERS.get(source_ref)
            if not adapter:
                for m in ms:
                    conn.execute("INSERT INTO ingest_runs(source,metric_id,run_at,status,rows_written,message) "
                                 "VALUES(?,?,?,?,?,?)", (source_ref, m["id"], now_utc, "failed", 0, "无对应adapter"))
                continue
            try:
                obs = adapter(ms)
            except Exception as e:
                print(f"!! [{source_ref}] 失败：{e}")
                for m in ms:
                    conn.execute("INSERT INTO ingest_runs(source,metric_id,run_at,status,rows_written,message) "
                                 "VALUES(?,?,?,?,?,?)", (source_ref, m["id"], now_utc, "failed", 0, str(e)[:200]))
                continue
            grp_by_metric = {}
            for o in obs:
                conn.execute(
                    "INSERT INTO observations(metric_id,obs_date,value,src_change,source,status,ingested_at) "
                    "VALUES(?,?,?,?,?,'ok',?) ON CONFLICT(metric_id,obs_date) DO UPDATE SET "
                    "value=excluded.value, src_change=excluded.src_change, "
                    "source=excluded.source, ingested_at=excluded.ingested_at",
                    (o["metric_id"], o["obs_date"], o["value"], o["src_change"], o["source"], now_utc))
                grp_by_metric.setdefault(o["metric_id"], []).append(o)
            for m in ms:
                got = grp_by_metric.get(m["id"], [])
                st = "ok" if got else "no_data"
                msg = ("dates=" + ",".join(sorted(x["obs_date"] for x in got)[-3:])) if got else "本次无该指标数据"
                conn.execute("INSERT INTO ingest_runs(source,metric_id,run_at,status,rows_written,message) "
                             "VALUES(?,?,?,?,?,?)", (source_ref, m["id"], now_utc, st, len(got), msg))
                all_by_metric[m["id"]] = got

        # 衍生指标（自算，读库计算，如现货升贴水 = 现货 − 主力）
        for m in METRICS:
            if not m.get("active", 1) or m["source_ref"] != "derived":
                continue
            p = m.get("params", {})
            rows = []
            if p.get("formula") == "spot_minus_futures":
                rows = conn.execute(
                    "SELECT a.obs_date, a.value - b.value FROM observations a "
                    "JOIN observations b ON a.obs_date = b.obs_date "
                    "WHERE a.metric_id=? AND b.metric_id=?",
                    (p["minuend"], p["subtrahend"])).fetchall()
            got = []
            for d, val in rows:
                conn.execute(
                    "INSERT INTO observations(metric_id,obs_date,value,src_change,source,status,ingested_at) "
                    "VALUES(?,?,?,?,?,'ok',?) ON CONFLICT(metric_id,obs_date) DO UPDATE SET "
                    "value=excluded.value, source=excluded.source, ingested_at=excluded.ingested_at",
                    (m["id"], d, val, None, "derived", now_utc))
                got.append({"metric_id": m["id"], "obs_date": d, "value": val, "src_change": None})
            st = "ok" if got else "no_data"
            conn.execute("INSERT INTO ingest_runs(source,metric_id,run_at,status,rows_written,message) "
                         "VALUES(?,?,?,?,?,?)", ("derived", m["id"], now_utc, st, len(got),
                                                 "computed" if got else "缺少输入数据"))
            all_by_metric[m["id"]] = got
        conn.commit()

        today = datetime.now(CN_TZ).date().isoformat()
        print(f"[{datetime.now(CN_TZ):%Y-%m-%d %H:%M} CST] 写入完成 -> {db_path}\n")
        for m in METRICS:
            got = sorted(all_by_metric.get(m["id"], []), key=lambda x: x["obs_date"])
            if got:
                latest = got[-1]
                tag = "（今日）" if latest["obs_date"] == today else f"（{latest['obs_date']}）"
                chg = "" if latest["src_change"] is None else f" ({latest['src_change']:+.1f})"
                print(f"  OK {m['name']:<8}{latest['value']:.1f} {m['unit']}{chg} {tag}  写入 {len(got)} 天")
            else:
                print(f"  -- {m['name']:<8}本次无数据")
        return 0
    finally:
        conn.close()


def main():
    global _SELFTEST
    if '--selftest' in sys.argv:
        _SELFTEST = True
        db = os.path.join(HERE, "data", "selftest.db")
        if os.path.exists(db):
            os.remove(db)
        print("[自测] Mysteel 用内置样本，akshare 源跳过（离线）\n")
        sys.exit(run(db))
    print(f"[{datetime.now(CN_TZ):%Y-%m-%d %H:%M} CST] 开始抓取 …")
    sys.exit(run(DB_PATH))


if __name__ == '__main__':
    main()
