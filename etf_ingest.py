#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETF 份额 + 融资融券 模块 · 采集落库（Tushare）

数据：
  - ETF 总份额：Tushare fund_share（单位 万份）。沪深300 ETF 6 只（固定），中证500 / 上证50 / 科创50 ETF 头部 6 只（首次运行按"份额×收盘价"自动挑选，
    结果写进 etf_config/etfs.json，之后一直沿用，保证图上的 ETF 集合稳定；想换就改这个文件）
  - 融资融券：Tushare margin（沪/深/北三所汇总，单位 元）
落库：data/etf.db   表：etf_funds / etf_share / margin / run_log
用法：
  python3 etf_ingest.py             # 增量（首次自动回填 3 年）
  python3 etf_ingest.py --selftest  # 离线自检（不联网）
环境变量：TUSHARE_TOKEN；TUSHARE_MAX_CALLS_PER_MIN（默认 45）
"""
import os
import re
import sys
import json
import time
import sqlite3
import threading
from collections import deque
from datetime import datetime, date, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "etf.db")
CFG_PATH = os.path.join(HERE, "etf_config", "etfs.json")
BACKFILL_DAYS = 365 * 3 + 5
OVERLAP_DAYS = 15
HS300 = ["510300.SH", "510310.SH", "510320.SH", "510330.SH", "510350.SH", "159919.SZ"]
N_CSI500 = 6
GROUPS = {"hs300": "沪深300 ETF", "sse50": "上证50 ETF", "csi500": "中证500 ETF", "kc50": "科创50 ETF"}
# 自动挑选规则：group → (基准里必须含有的关键词, 名称里必须含有的关键词, 额外排除的名称正则)
PICK_RULES = {
    "csi500": ("中证500", "500", None),
    "sse50": ("上证50指数", "50", r"科创|创业|中证|等权|深证|沪深"),
    "kc50": ("科创板50", "50", r"创业|科创100|科创200|中证|综指"),
}
# 仅用于"规模排名日志"（不用于自动挑选）：沪深300 也列出来，方便核对页面上的清单是否漏了大的 ETF
RANK_RULES = dict(PICK_RULES)
RANK_RULES["hs300"] = ("沪深300", "300", r"创业|科创|中证|等权|深证")

# ---------------- Tushare 限速 + 超限重试 ----------------
_TS_MAX = int(os.environ.get("TUSHARE_MAX_CALLS_PER_MIN", "45"))
_lock = threading.Lock()
_calls = deque()


def _throttle():
    while True:
        with _lock:
            now = time.monotonic()
            while _calls and now - _calls[0] >= 60.0:
                _calls.popleft()
            if len(_calls) < _TS_MAX:
                _calls.append(now)
                return
            wait = 60.0 - (now - _calls[0]) + 0.2
        time.sleep(max(wait, 0.2))


def _is_rate_limit(e):
    m = str(e).lower()
    return any(h in m for h in ("频率超限", "每分钟", "次/分钟", "访问接口", "too many", "rate limit"))


def ts_call(fn, *a, **kw):
    for attempt in range(6):
        _throttle()
        try:
            return fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            if not _is_rate_limit(e) or attempt == 5:
                raise
            print(f"  ⏳ Tushare 频率超限，等待 62 秒重试 ({attempt + 1}/5)...", flush=True)
            time.sleep(62)
            with _lock:
                _calls.clear()


def _ymd(d):
    return d.strftime("%Y%m%d")


def _iso(s):
    s = str(s)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def _f(v):
    try:
        return None if v is None or v != v else float(v)
    except (TypeError, ValueError):
        return None


# ---------------- 库 ----------------
def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS etf_funds(
      code TEXT PRIMARY KEY, name TEXT, grp TEXT NOT NULL, sort INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS etf_share(
      code TEXT NOT NULL, date TEXT NOT NULL, share REAL NOT NULL, PRIMARY KEY(code, date));
    CREATE TABLE IF NOT EXISTS margin(
      date TEXT NOT NULL, exch TEXT NOT NULL,
      rzye REAL, rzmre REAL, rzche REAL, rqye REAL, rqmcl REAL, rzrqye REAL, rqyl REAL,
      PRIMARY KEY(date, exch));
    CREATE TABLE IF NOT EXISTS run_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, item TEXT, status TEXT, rows INTEGER, message TEXT);
    """)
    conn.commit()


def log(conn, item, status, rows, msg=""):
    conn.execute("INSERT INTO run_log(run_at,item,status,rows,message) VALUES(?,?,?,?,?)",
                 (datetime.now(timezone.utc).isoformat(timespec="seconds"), item, status, rows, msg[:300]))


# ---------------- 配置：挑选中证500 头部 ETF ----------------
_EXCL = re.compile(r"增强|质量|价值|成长|现金流|低波|红利|ESG|等权|联接|LOF|基本面|动量|因子|量化|医药|消费|科技|周期")


def rank_funds(pro, basic, group, rules=None):
    """候选：基准为目标指数、名称里不含增强/风格后缀的 ETF；按 份额×收盘价（≈规模）从大到小。返回 [(规模万元, code, name)]。"""
    bench_kw, name_kw, extra_excl = (rules or PICK_RULES)[group]
    cand = basic[basic["benchmark"].astype(str).str.contains(bench_kw, na=False)
                 & basic["name"].astype(str).str.contains(name_kw)
                 & ~basic["name"].astype(str).str.contains(_EXCL)]
    if extra_excl:
        cand = cand[~cand["name"].astype(str).str.contains(extra_excl)]
    cand = cand[cand["name"].astype(str).str.contains("ETF")]
    start, end = _ymd(date.today() - timedelta(days=14)), _ymd(date.today())
    scored = []
    for code, name in cand[["ts_code", "name"]].values.tolist():
        try:
            sh = ts_call(pro.fund_share, ts_code=code, start_date=start, end_date=end)
            px = ts_call(pro.fund_daily, ts_code=code, start_date=start, end_date=end)
            if len(sh) and len(px):
                s = sh.sort_values("trade_date").iloc[-1]["fd_share"]
                c = px.sort_values("trade_date").iloc[-1]["close"]
                scored.append((float(s) * float(c), code, name))
        except Exception as ex:  # noqa: BLE001
            print(f"  ⚠ 候选 {code} 取数失败：{str(ex)[:80]}")
    scored.sort(reverse=True)
    return scored


def pick_funds(pro, basic, group, n=N_CSI500):
    scored = rank_funds(pro, basic, group)
    print(f"  {GROUPS[group]} 候选规模排名（份额万份×收盘价，单位≈万元）：")
    for sc, code, name in scored[:12]:
        print(f"    {code}  {name:<22} {sc / 1e4:>10.1f} 亿元")
    return [c for _, c, _ in scored[:n]]


def print_rankings(pro, basic, cfg):
    """手动运行（workflow_dispatch）或 ETF_RANK=1 时，打印四个指数的全市场 ETF 规模前 15，★ = 已在页面清单里。"""
    for g in GROUPS:
        try:
            scored = rank_funds(pro, basic, g, RANK_RULES)
        except Exception as ex:  # noqa: BLE001
            print(f"  ⚠ {GROUPS[g]} 规模排名失败：{str(ex)[:100]}")
            continue
        print(f"\n【规模排名】{GROUPS[g]}（份额×收盘价，亿元；★=在页面清单里，共 {len(scored)} 只候选）")
        for i, (sc, code, name) in enumerate(scored[:15], 1):
            print(f"  {i:>2}. {'★' if code in cfg.get(g, []) else ' '} {code:<10} {name:<24} {sc / 1e4:>9.1f}")
        miss = [(i, code, name) for i, (_, code, name) in enumerate(scored[:6], 1) if code not in cfg.get(g, [])]
        if miss:
            print("  ⚠ 规模前6但不在页面清单：" + "、".join(f"第{i}名 {c} {n}" for i, c, n in miss))
    print()


def load_config(pro, basic):
    cfg = {}
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    if not cfg.get("hs300"):
        cfg["hs300"] = HS300
    for g in PICK_RULES:                                # 某组还没有清单（含新增的组）→ 自动挑选一次，之后沿用
        if not cfg.get(g):
            cfg[g] = pick_funds(pro, basic, g)
            print(f"  已自动选定{GROUPS[g]}：", cfg[g])
    out = {g: [c.strip() for c in cfg[g] if str(c).strip()] for g in GROUPS}
    names = dict(basic[["ts_code", "name"]].values.tolist()) if basic is not None and len(basic) else {}
    to_save = {g: out[g] for g in GROUPS}
    to_save["_说明"] = "各组 ETF 代码清单。想增删/替换就改这里（保持 代码.SH / 代码.SZ 格式），下次运行生效；某组留空则自动重新挑选。"
    os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(to_save, f, ensure_ascii=False, indent=1)
    return out, names


# ---------------- 主流程 ----------------
def last_date(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return r[0] if r and r[0] else None


def run(db_path, pro, offline=False):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        today = datetime.now(CN_TZ).date()
        basic = ts_call(pro.fund_basic, market="E", status="L")
        cfg, names = load_config(pro, basic)
        n_ok = n_fail = 0
        if not offline and (os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" or os.environ.get("ETF_RANK") == "1"):
            print_rankings(pro, basic, cfg)

        # 清单（含名称）
        conn.execute("DELETE FROM etf_funds")
        for g, codes in cfg.items():
            for i, c in enumerate(codes):
                conn.execute("INSERT OR REPLACE INTO etf_funds(code,name,grp,sort) VALUES(?,?,?,?)",
                             (c, names.get(c, c), g, i))

        # ETF 份额
        for g, codes in cfg.items():
            for c in codes:
                last = last_date(conn, "SELECT MAX(date) FROM etf_share WHERE code=?", (c,))
                start = (datetime.strptime(last, "%Y-%m-%d").date() - timedelta(days=OVERLAP_DAYS)) if last \
                    else today - timedelta(days=BACKFILL_DAYS)
                try:
                    df = ts_call(pro.fund_share, ts_code=c, start_date=_ymd(start), end_date=_ymd(today))
                    n = 0
                    for d, s in df[["trade_date", "fd_share"]].values.tolist():
                        if _f(s) is not None:
                            conn.execute("INSERT INTO etf_share(code,date,share) VALUES(?,?,?) "
                                         "ON CONFLICT(code,date) DO UPDATE SET share=excluded.share", (c, _iso(d), float(s)))
                            n += 1
                    log(conn, f"share:{c}", "ok", n, f"from {start}")
                    n_ok += 1
                    print(f"  {g:<7}{c:<11}{names.get(c, ''):<20} +{n} 行")
                except Exception as ex:  # noqa: BLE001
                    n_fail += 1
                    log(conn, f"share:{c}", "failed", 0, str(ex))
                    print(f"  ⚠ {c} 份额取数失败：{str(ex)[:120]}")

        # 融资融券
        last = last_date(conn, "SELECT MAX(date) FROM margin")
        start = (datetime.strptime(last, "%Y-%m-%d").date() - timedelta(days=OVERLAP_DAYS)) if last \
            else today - timedelta(days=BACKFILL_DAYS)
        try:
            df = ts_call(pro.margin, start_date=_ymd(start), end_date=_ymd(today))
            n = 0
            for r in df.to_dict("records"):
                conn.execute("INSERT INTO margin(date,exch,rzye,rzmre,rzche,rqye,rqmcl,rzrqye,rqyl) VALUES(?,?,?,?,?,?,?,?,?) "
                             "ON CONFLICT(date,exch) DO UPDATE SET rzye=excluded.rzye,rzmre=excluded.rzmre,rzche=excluded.rzche,"
                             "rqye=excluded.rqye,rqmcl=excluded.rqmcl,rzrqye=excluded.rzrqye,rqyl=excluded.rqyl",
                             (_iso(r["trade_date"]), r["exchange_id"], _f(r.get("rzye")), _f(r.get("rzmre")), _f(r.get("rzche")),
                              _f(r.get("rqye")), _f(r.get("rqmcl")), _f(r.get("rzrqye")), _f(r.get("rqyl"))))
                n += 1
            log(conn, "margin", "ok", n, f"from {start}")
            n_ok += 1
            print(f"  融资融券  +{n} 行")
        except Exception as ex:  # noqa: BLE001
            n_fail += 1
            log(conn, "margin", "failed", 0, str(ex))
            print(f"  ⚠ 融资融券取数失败：{str(ex)[:120]}")

        conn.commit()
        for g in GROUPS:
            print(f"  [{GROUPS[g]}] 最新份额日期：",
                  last_date(conn, "SELECT MAX(date) FROM etf_share WHERE code IN (SELECT code FROM etf_funds WHERE grp=?)", (g,)))
        print("  [融资融券] 最新日期：", last_date(conn, "SELECT MAX(date) FROM margin"))
        print(f"完成：成功 {n_ok} 项，失败 {n_fail} 项")
        return 1 if (n_ok == 0 and n_fail) else 0
    finally:
        conn.close()


# ---------------- 离线自检 ----------------
class _FakePro:
    """内存里造一份形状与 Tushare 一致的数据。"""
    def __init__(self):
        import pandas as pd
        self.pd = pd
        self.days = [d for d in (date.today() - timedelta(days=i) for i in range(120, -1, -1)) if d.weekday() < 5]

    def fund_basic(self, **kw):
        rows = [(c, n, "沪深300指数收益率") for c, n in zip(HS300, ["华泰柏瑞沪深300ETF", "易方达沪深300ETF", "中金沪深300ETF",
                                                              "华夏沪深300ETF", "工银沪深300ETF", "嘉实沪深300ETF"])]
        rows += [("510500.SH", "南方中证500ETF", "中证500指数收益率"), ("159922.SZ", "嘉实中证500ETF", "中证500指数收益率"),
                 ("510050.SH", "华夏上证50ETF", "上证50指数收益率"), ("510710.SH", "博时上证50ETF", "上证50指数收益率"),
                 ("510850.SH", "工银上证50ETF", "上证50指数收益率"), ("510100.SH", "南方上证50ETF", "上证50指数收益率"),
                 ("588080.SH", "易方达科创50ETF", "上证科创板50成份指数收益率"), ("588000.SH", "华夏科创50ETF", "上证科创板50成份指数收益率"),
                 ("588200.SH", "嘉实科创芯片ETF", "上证科创板芯片指数收益率"), ("588360.SH", "科创创业50ETF", "中证科创创业50指数收益率"), ("510680.SH", "万家上证50增强ETF", "上证50指数收益率"),
                 ("512500.SH", "华夏中证500ETF", "中证500指数收益率"), ("510510.SH", "广发中证500ETF", "中证500指数收益率"),
                 ("159820.SZ", "天弘中证500ETF", "中证500指数收益率"), ("512510.SH", "华泰柏瑞中证500ETF", "中证500指数收益率"),
                 ("159337.SZ", "东财中证500ETF", "中证500指数收益率"), ("561950.SH", "招商中证500增强策略ETF", "中证500指数收益率"),
                 ("562340.SH", "银华中证500质量成长ETF", "中证500指数收益率")]
        return self.pd.DataFrame(rows, columns=["ts_code", "name", "benchmark"])

    def _span(self, kw):
        s = kw.get("start_date", "00000000"); e = kw.get("end_date", "99999999")
        return [d for d in self.days if s <= _ymd(d) <= e]

    def fund_share(self, ts_code, **kw):
        seed = sum(map(ord, ts_code)) % 97 + 50
        rows = [(ts_code, _ymd(d), (seed * 1e4) * (1 + 0.002 * i) + (i % 7) * 1e3, "ETF", "SH") for i, d in enumerate(self.days) if d in self._span(kw)]
        return self.pd.DataFrame(rows, columns=["ts_code", "trade_date", "fd_share", "fund_type", "market"])

    def fund_daily(self, ts_code, **kw):
        rows = [(ts_code, _ymd(d), 4.0) for d in self._span(kw)]
        return self.pd.DataFrame(rows, columns=["ts_code", "trade_date", "close"])

    def margin(self, **kw):
        rows = []
        for i, d in enumerate(self.days):
            if d not in self._span(kw):
                continue
            for ex, k in (("SSE", 1.2e12), ("SZSE", 1.1e12), ("BSE", 2e9)):
                rows.append((_ymd(d), ex, k + i * 1e9, 5e10 + i * 1e8, 4.8e10, 1.5e10, 2e7, k + i * 1e9 + 1.5e10, 3e9))
        return self.pd.DataFrame(rows, columns=["trade_date", "exchange_id", "rzye", "rzmre", "rzche", "rqye", "rqmcl", "rzrqye", "rqyl"])


def selftest():
    global CFG_PATH
    import tempfile
    tmp = tempfile.mkdtemp()
    CFG_PATH = os.path.join(tmp, "etfs.json")
    db = os.path.join(tmp, "etf.db")
    pro = _FakePro()
    assert run(db, pro) == 0
    conn = sqlite3.connect(db)
    q = lambda s: conn.execute(s).fetchone()[0]
    assert q("SELECT COUNT(*) FROM etf_funds WHERE grp='hs300'") == 6
    assert q("SELECT COUNT(*) FROM etf_funds WHERE grp='csi500'") == 6
    sse = {r[0] for r in conn.execute("SELECT code FROM etf_funds WHERE grp='sse50'")}
    assert sse == {"510050.SH", "510710.SH", "510850.SH", "510100.SH"}, sse       # 科创50、增强 被排除
    picked = {r[0] for r in conn.execute("SELECT code FROM etf_funds WHERE grp='csi500'")}
    assert "561950.SH" not in picked and "562340.SH" not in picked, picked      # 增强/风格 ETF 被排除
    kc = {r[0] for r in conn.execute("SELECT code FROM etf_funds WHERE grp='kc50'")}
    assert kc == {"588080.SH", "588000.SH"}, kc                                      # 科创创业50、芯片 被排除
    n1 = q("SELECT COUNT(*) FROM etf_share"); m1 = q("SELECT COUNT(*) FROM margin")
    assert n1 > 500 and m1 > 100
    conn.close()
    assert run(db, pro) == 0                                                       # 第二次：增量，不重复
    conn = sqlite3.connect(db)
    assert q("SELECT COUNT(*) FROM etf_share") == n1 and q("SELECT COUNT(*) FROM margin") == m1
    cfg = json.load(open(CFG_PATH, encoding="utf-8"))
    assert len(cfg["csi500"]) == 6 and cfg["hs300"] == HS300 and len(cfg["sse50"]) == 4 and len(cfg["kc50"]) == 2
    print("selftest OK")


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    tok = os.environ.get("TUSHARE_TOKEN", "")
    if not tok:
        sys.exit("环境变量 TUSHARE_TOKEN 未设置")
    import tushare as ts
    ts.set_token(tok)
    print(f"[{datetime.now(CN_TZ):%Y-%m-%d %H:%M} CST] 开始 …")
    sys.exit(run(DB_PATH, ts.pro_api()))


if __name__ == "__main__":
    main()
