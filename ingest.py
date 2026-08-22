#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A-1 落库脚本：抓取 Mysteel 电解铝价格/库存 → 写入 SQLite → 记抓取日志。

用法：
    python3 ingest.py             # 联网抓取今天的数据，写入 data/portfolio.db
    python3 ingest.py --selftest  # 不联网，用内置真实样本（8/19~8/21）写入 data/selftest.db

设计纪律（见设计文档第十一节）：
  · 时区：obs_date 用北京交易日；ingested_at 用 UTC 时间戳
  · 溯源：每条观测记 source 与 ingested_at
  · 幂等：(metric_id, obs_date) 主键 upsert，重复跑结果一致
  · 配置驱动：模块/指标写在配置里，加指标=加配置
  · 日志：每个指标每次抓取记一条 ingest_runs，支撑将来的“数据状态”页
零依赖：仅用 Python 自带库。
"""
import os
import re
import sys
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen

CN_TZ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "portfolio.db")

URL = ("https://openapi.mysteel.com/without_sign/newsflash/flashnews/query_by_tags.htm"
       "?advertisementFlag=0&keyword=&pageNo=1&pageSize=30&sortByScore=false"
       "&columnIds=%255B%255B2%252C84%252C584%255D%255D&breedTagId=4437")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://www.mysteel.com/fastcomment/",
    "Accept": "application/json, text/plain, */*",
}

# ---------- 配置：模块与指标（配置驱动，加指标=在这加一行）----------
MODULE = {"id": "aluminum", "name": "电解铝", "sort_order": 1}
METRICS = [
    {"id": "al_spot_east",    "name": "现货·华东", "category": "price",     "unit": "元/吨", "update_freq": "daily"},
    {"id": "al_spot_south",   "name": "现货·华南", "category": "price",     "unit": "元/吨", "update_freq": "daily"},
    {"id": "al_spot_central", "name": "现货·中原", "category": "price",     "unit": "元/吨", "update_freq": "daily"},
    {"id": "al_social_inv",   "name": "社会库存",  "category": "inventory", "unit": "万吨", "update_freq": "biweekly"},
]
REGION_TO_METRIC = {"华东": "al_spot_east", "华南": "al_spot_south", "中原": "al_spot_central"}
SOURCE_TYPE, SOURCE_REF, SOURCE_NAME = "api", "mysteel_flash", "mysteel_openapi"

# ---------- 解析（全/半角标点兼容）----------
_REGION_RE = re.compile(
    r'(华东|华南|华北|中原|西南|西北|东北)\s*([\d]+(?:\.\d+)?)\s*(?:元/吨)?\s*[，,]\s*'
    r'(涨|跌|平|持平|稳)\s*([\d]+(?:\.\d+)?)?')
_INV_TOTAL_RE = re.compile(r'库存总量为\s*([\d.]+)\s*万吨')
_INV_CHANGE_RE = re.compile(r'较上期数据\s*(增加|减少|下降|持平)?\s*([\d.]+)?\s*万吨')


def _ts_to_cn_date(ms):
    return datetime.fromtimestamp(ms / 1000, tz=CN_TZ).date().isoformat()


def parse_flash(content, obs_date):
    """一条快讯 → [(metric_id, value, src_change), ...]"""
    out = []
    if '铝锭价格行情' in content:
        for region, price, direction, amt in _REGION_RE.findall(content):
            mid = REGION_TO_METRIC.get(region)
            if not mid:                      # 未配置的地区（如华北）直接跳过——配置驱动
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


# ---------- 采集适配器（返回观测列表；将来加源=加一个这样的函数）----------
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


def adapter_mysteel_flash(raw_json):
    """把接口返回解析成观测：[{metric_id, obs_date, value, src_change, source}]"""
    items = _find_items(raw_json)
    if items is None:
        raise ValueError("返回里找不到 content 列表，接口结构可能变了")
    obs = []
    for it in items:
        content = it.get('content', '')
        ms = it.get('publisherTime')
        obs_date = _ts_to_cn_date(ms) if ms else datetime.now(CN_TZ).date().isoformat()
        for metric_id, value, src_change in parse_flash(content, obs_date):
            obs.append({"metric_id": metric_id, "obs_date": obs_date,
                        "value": value, "src_change": src_change, "source": SOURCE_NAME})
    return obs


def fetch_raw():
    req = Request(URL, headers=HEADERS)
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))


# ---------- 数据库 ----------
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
    # 配置 upsert（幂等）
    conn.execute("INSERT INTO modules(id,name,sort_order) VALUES(?,?,?) "
                 "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                 (MODULE["id"], MODULE["name"], MODULE["sort_order"]))
    for i, m in enumerate(METRICS):
        conn.execute(
            "INSERT INTO metrics(id,module_id,name,category,unit,source_type,source_ref,update_freq,sort_order) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "name=excluded.name, unit=excluded.unit, category=excluded.category",
            (m["id"], MODULE["id"], m["name"], m["category"], m["unit"],
             SOURCE_TYPE, SOURCE_REF, m["update_freq"], i))
    conn.commit()


def upsert_observation(conn, o, now_utc):
    conn.execute(
        "INSERT INTO observations(metric_id,obs_date,value,src_change,source,status,ingested_at) "
        "VALUES(?,?,?,?,?,'ok',?) ON CONFLICT(metric_id,obs_date) DO UPDATE SET "
        "value=excluded.value, src_change=excluded.src_change, "
        "source=excluded.source, ingested_at=excluded.ingested_at",
        (o["metric_id"], o["obs_date"], o["value"], o["src_change"], o["source"], now_utc))


def log_run(conn, metric_id, status, rows, msg, now_utc):
    conn.execute("INSERT INTO ingest_runs(source,metric_id,run_at,status,rows_written,message) "
                 "VALUES(?,?,?,?,?,?)", (SOURCE_REF, metric_id, now_utc, status, rows, msg))


def run(db_path, raw_json):
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        try:
            obs = adapter_mysteel_flash(raw_json)
        except Exception as e:
            for m in METRICS:
                log_run(conn, m["id"], "failed", 0, str(e), now_utc)
            conn.commit()
            print(f"!! 采集/解析失败：{e}")
            return 2

        by_metric = {}
        for o in obs:
            upsert_observation(conn, o, now_utc)
            by_metric.setdefault(o["metric_id"], []).append(o)

        # 每个配置指标记一条日志（有数据=ok，没数据=no_data）
        for m in METRICS:
            got = by_metric.get(m["id"], [])
            if got:
                dates = ",".join(sorted(x["obs_date"] for x in got))
                log_run(conn, m["id"], "ok", len(got), f"dates={dates}", now_utc)
            else:
                log_run(conn, m["id"], "no_data", 0, "本次无该指标快讯", now_utc)
        conn.commit()

        # 屏幕反馈
        today = datetime.now(CN_TZ).date().isoformat()
        print(f"[{datetime.now(CN_TZ):%Y-%m-%d %H:%M} CST] 写入完成 → {db_path}\n")
        for m in METRICS:
            got = sorted(by_metric.get(m["id"], []), key=lambda x: x["obs_date"])
            if got:
                latest = got[-1]
                tag = "（今日）" if latest["obs_date"] == today else f"（{latest['obs_date']}）"
                unit = m["unit"]
                print(f"  ✓ {m['name']:<8}{latest['value']:.1f} {unit} "
                      f"({latest['src_change']:+.1f}) {tag}  本次写入 {len(got)} 天")
            else:
                print(f"  · {m['name']:<8}本次无数据")
        return 0
    finally:
        conn.close()


# 内置真实样本（你这一路贴过的真实快讯：8/19~8/21 价格 + 8/20 库存）
SAMPLE = {"data": {"list": [
    {"content": "8月21日Mysteel铝锭价格行情:华东23680,涨80;华南23850,涨140;中原23620,涨90(单位:元/吨)",
     "publisherTime": 1787279319755},
    {"content": "Mysteel库存速递：中国铝锭现货库存总量为84.9万吨，较上期数据减少1.4万吨。",
     "publisherTime": 1787187503324},
    {"content": "8月20日Mysteel铝锭价格行情：华东23600，跌70；华南23710，跌80；中原23530，跌60（单位：元/吨）",
     "publisherTime": 1787192923000},
    {"content": "8月19日Mysteel铝锭价格行情：华东23670，跌230；华南23790，跌240；中原23590，跌210（单位：元/吨）",
     "publisherTime": 1787106523000},
    {"content": "8月20日Mysteel铝棒加工费行情：山东500，涨20", "publisherTime": 1787192000000},
]}}


def main():
    if '--selftest' in sys.argv:
        db = os.path.join(HERE, "data", "selftest.db")
        if os.path.exists(db):
            os.remove(db)
        print("[自测] 用内置真实样本（8/19~8/21）写入独立库，不联网\n")
        sys.exit(run(db, SAMPLE))

    print(f"[{datetime.now(CN_TZ):%Y-%m-%d %H:%M} CST] 请求 Mysteel 接口 …")
    try:
        raw = fetch_raw()
    except Exception as e:
        print(f"!! 请求失败：{e}\n   可稍后重试，或先 python3 ingest.py --selftest 验证逻辑。")
        sys.exit(1)
    sys.exit(run(DB_PATH, raw))


if __name__ == '__main__':
    main()
