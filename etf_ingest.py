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
GROUPS = {"hs300": "沪深300 ETF", "sse50": "上证50 ETF", "csi500": "中证500 ETF", "kc50": "科创50 ETF", "cyb": "创业板 ETF"}
# 自动挑选规则：group → (基准里必须含有的关键词, 名称里必须含有的关键词, 额外排除的名称正则)
PICK_RULES = {
    "csi500": ("中证500", "500", None),
    "sse50": ("上证50指数", "50", r"科创|创业|中证|等权|深证|沪深"),
    "kc50": ("科创板50", "50", r"创业|科创100|科创200|中证|综指"),
    "cyb": ("创业板", "创业板", r"50|综指|人工智能|新能源|软件|大数据|半导体|芯片|电池|机器人|AI|创业板50"),
}
# 仅用于"规模排名日志"（不用于自动挑选）：沪深300 也列出来，方便核对页面上的清单是否漏了大的 ETF
RANK_RULES = dict(PICK_RULES)
RANK_RULES["hs300"] = ("沪深300", "300", r"创业|科创|中证|等权|深证|非银|金融|银行|地产|医药|消费|红利")
# 已知的大规模 ETF：不管 fund_basic 的基准/名称字段怎么写，排名日志里都强制带上并打印它们在 fund_basic 里的样子（排查"漏选"）
WATCH = {"hs300": ["510320.SH", "510360.SH"], "sse50": ["510050.SH"],
         "csi500": ["510500.SH", "512500.SH", "159922.SZ", "510580.SH", "510510.SH"], "kc50": [],
         "cyb": ["159915.SZ", "159949.SZ", "159952.SZ", "159977.SZ", "159948.SZ", "159957.SZ"]}

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
    CREATE TABLE IF NOT EXISTS etf_price(
      code TEXT NOT NULL, date TEXT NOT NULL, close REAL NOT NULL, PRIMARY KEY(code, date));
    CREATE TABLE IF NOT EXISTS margin(
      date TEXT NOT NULL, exch TEXT NOT NULL,
      rzye REAL, rzmre REAL, rzche REAL, rqye REAL, rqmcl REAL, rzrqye REAL, rqyl REAL,
      PRIMARY KEY(date, exch));
    CREATE TABLE IF NOT EXISTS hs300_members(
      snap TEXT NOT NULL, code TEXT NOT NULL, PRIMARY KEY(snap, code));
    CREATE TABLE IF NOT EXISTS hs300_mv(
      date TEXT PRIMARY KEY, float_mv REAL NOT NULL, n_members INTEGER, n_matched INTEGER);
    CREATE TABLE IF NOT EXISTS margin_account(
      date TEXT PRIMARY KEY, avg_ratio REAL, guarantee REAL);
    CREATE TABLE IF NOT EXISTS turnover(
      date TEXT PRIMARY KEY, sh REAL, sz REAL, bj REAL, sh_close REAL);
    CREATE TABLE IF NOT EXISTS run_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, item TEXT, status TEXT, rows INTEGER, message TEXT);
    """)
    conn.commit()


def log(conn, item, status, rows, msg=""):
    conn.execute("INSERT INTO run_log(run_at,item,status,rows,message) VALUES(?,?,?,?,?)",
                 (datetime.now(timezone.utc).isoformat(timespec="seconds"), item, status, rows, msg[:300]))


# ---------------- 配置：挑选中证500 头部 ETF ----------------
_EXCL = re.compile(r"增强|质量|价值|成长|现金流|低波|红利|ESG|等权|联接|LOF|基本面|动量|因子|量化|医药|消费|科技|周期")


def rank_funds(pro, basic, group, rules=None, extra=()):
    """候选：基准为目标指数、名称里不含增强/风格后缀的 ETF；按 份额×收盘价（≈规模）从大到小。返回 [(规模万元, code, name)]。"""
    bench_kw, name_kw, extra_excl = (rules or PICK_RULES)[group]
    cand = basic[basic["benchmark"].astype(str).str.contains(bench_kw, na=False)
                 & basic["name"].astype(str).str.contains(name_kw)
                 & ~basic["name"].astype(str).str.contains(_EXCL)]
    if extra_excl:
        cand = cand[~cand["name"].astype(str).str.contains(extra_excl)]
    cand = cand[cand["name"].astype(str).str.contains("ETF")]
    pairs = cand[["ts_code", "name"]].values.tolist()
    seen = {c for c, _ in pairs}
    nm = dict(basic[["ts_code", "name"]].values.tolist())
    for c in extra:                                        # 规则外的指定代码（仅排名日志用）
        if c not in seen:
            pairs.append((c, "(规则外)" + str(nm.get(c, "不在fund_basic里"))))
    start, end = _ymd(date.today() - timedelta(days=14)), _ymd(date.today())
    scored = []
    for code, name in pairs:
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
            scored = rank_funds(pro, basic, g, RANK_RULES, extra=list(cfg.get(g, [])) + WATCH.get(g, []))
        except Exception as ex:  # noqa: BLE001
            print(f"  ⚠ {GROUPS[g]} 规模排名失败：{str(ex)[:100]}")
            continue
        print(f"\n【规模排名】{GROUPS[g]}（份额×收盘价，亿元；★=在页面清单里，共 {len(scored)} 只候选）")
        for i, (sc, code, name) in enumerate(scored[:15], 1):
            print(f"  {i:>2}. {'★' if code in cfg.get(g, []) else ' '} {code:<10} {name:<24} {sc / 1e4:>9.1f}")
        ranked = {c for _, c, _ in scored}
        for c in list(cfg.get(g, [])) + WATCH.get(g, []):
            if c not in ranked:
                r = basic[basic["ts_code"] == c]
                info = r[["name", "benchmark"]].values.tolist()[0] if len(r) else "不在 fund_basic 里"
                print(f"  ⚠ {c} 没取到份额/收盘价，fund_basic 里是：{info}")
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


# ---------------- 沪深300 流通市值（成分股 circ_mv 求和，单位 万元）----------------
HS300_INDEX = "000300.SH"
MV_MAX_PER_RUN = 450            # 每次最多补这么多个交易日的 daily_basic（每天 1 次调用，防止首次回填超时；下次接着补）
MV_MIN_MATCH = 0.9              # 当日成分股里至少 90% 在 daily_basic 里找到，否则该日不入库


def update_hs300_mv(conn, pro, today):
    """成分股快照：index_weight（月度）；流通市值：对每个交易日取 daily_basic 全市场 circ_mv，按当时的成分股求和。"""
    start_d = today - timedelta(days=BACKFILL_DAYS)
    # 1) 成分股快照（从已有最新快照前推 40 天起补；首次从起点前 60 天起，按 90 天一段取）
    last_snap = last_date(conn, "SELECT MAX(snap) FROM hs300_members")
    cur = (datetime.strptime(last_snap, "%Y-%m-%d").date() - timedelta(days=40)) if last_snap else start_d - timedelta(days=60)
    n_snap = 0
    while cur <= today:
        end = min(cur + timedelta(days=90), today)
        df = ts_call(pro.index_weight, index_code=HS300_INDEX, start_date=_ymd(cur), end_date=_ymd(end))
        for sn, code in df[["trade_date", "con_code"]].values.tolist():
            conn.execute("INSERT OR IGNORE INTO hs300_members(snap,code) VALUES(?,?)", (_iso(sn), code)); n_snap += 1
        cur = end + timedelta(days=1)
    snaps = [r[0] for r in conn.execute("SELECT DISTINCT snap FROM hs300_members ORDER BY snap")]
    if not snaps:
        raise ValueError("index_weight 没有返回沪深300成分股（可能积分/权限不足）")
    members = {sn: {r[0] for r in conn.execute("SELECT code FROM hs300_members WHERE snap=?", (sn,))} for sn in snaps}
    # 2) 需要补的交易日：etf_price 里有、hs300_mv 里还没有；先补最近的
    need = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM etf_price WHERE date>=? AND date NOT IN (SELECT date FROM hs300_mv) ORDER BY date DESC",
        (start_d.isoformat(),))][:MV_MAX_PER_RUN]
    n_ok = n_skip = 0
    for d in need:
        prior = [sn for sn in snaps if sn <= d]
        mem = members[prior[-1]] if prior else members[snaps[0]]          # 早于首个快照的日子，用最早的快照近似
        df = ts_call(pro.daily_basic, trade_date=d.replace("-", ""), fields="ts_code,trade_date,circ_mv")
        fm = {c: _f(v) for c, v in df[["ts_code", "circ_mv"]].values.tolist()}      # 股票 daily_basic 的流通市值列叫 circ_mv（万元）
        got = [fm[c] for c in mem if fm.get(c) is not None]
        if len(mem) and len(got) / len(mem) >= MV_MIN_MATCH:
            conn.execute("INSERT OR REPLACE INTO hs300_mv(date,float_mv,n_members,n_matched) VALUES(?,?,?,?)",
                         (d, float(sum(got)), len(mem), len(got))); n_ok += 1
        else:
            n_skip += 1
    conn.commit()
    return n_snap, n_ok, n_skip, len(need)


# ---------------- 两融账户统计：平均维持担保比例（东方财富数据中心，日度）----------------
ACCOUNT_URL = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_WEB_MARGIN_DAILYTRADE"
               "&columns=ALL&pageSize=500&sortColumns=STATISTICS_DATE&sortTypes=-1&pageNumber=")


# ---------------- A股每日成交额 ----------------
TURNOVER_START = "2016-01-01"
TURNOVER_IDX = {"sh": "000001.SH", "sz": "399106.SZ", "bj": "899050.BJ"}   # 上证综指 / 深证综指 / 北证50（全市场口径近似）


def update_turnover(conn, pro, today):
    """三个指数的 amount（千元）→ 亿元。沪、深必须成功；北交所取不到只记警告。返回写入行数。"""
    last = last_date(conn, "SELECT MAX(date) FROM turnover")
    start = (datetime.strptime(last, "%Y-%m-%d").date() - timedelta(days=OVERLAP_DAYS)).isoformat() if last else TURNOVER_START
    got = {}
    for k, code in TURNOVER_IDX.items():
        try:
            df = ts_call(pro.index_daily, ts_code=code, start_date=start.replace("-", ""), end_date=_ymd(today),
                         fields="ts_code,trade_date,close,amount")
        except Exception as ex:  # noqa: BLE001
            if k != "bj":
                raise
            print(f"  ⚠ 北交所成交额取数失败（忽略）：{str(ex)[:100]}")
            continue
        got[k] = {_iso(str(r[0])): (_f(r[1]), _f(r[2])) for r in df[["trade_date", "close", "amount"]].values.tolist()} \
            if df is not None and len(df) else {}
    if not got.get("sh") or not got.get("sz"):
        raise RuntimeError("上证/深证指数成交额为空")
    n = 0
    for d in sorted(set(got["sh"]) & set(got["sz"])):
        a = lambda k: (got.get(k, {}).get(d, (None, None))[1])
        conv = lambda v: None if v is None else round(v / 1e5, 2)          # 千元 → 亿元
        conn.execute("INSERT INTO turnover(date,sh,sz,bj,sh_close) VALUES(?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
                     "sh=excluded.sh, sz=excluded.sz, bj=excluded.bj, sh_close=excluded.sh_close",
                     (d, conv(a("sh")), conv(a("sz")), conv(a("bj")), got["sh"][d][0]))
        n += 1
    conn.commit()
    return n


def fetch_account_info(since):
    """返回 [(YYYY-MM-DD, 平均维持担保比例, 担保物总价值(元))]，只取 since（YYYY-MM-DD）之后。按日期倒序分页，翻到早于 since 就停。"""
    from urllib.request import Request, urlopen
    out, page, pages = [], 1, 1
    while page <= pages:
        last_err = None
        for _ in range(3):
            try:
                req = Request(ACCOUNT_URL + str(page), headers={"User-Agent": "Mozilla/5.0"})
                j = json.loads(urlopen(req, timeout=60).read().decode("utf-8"))
                break
            except Exception as ex:  # noqa: BLE001
                last_err = ex; time.sleep(3)
        else:
            raise last_err
        res = j.get("result") or {}
        pages = int(res.get("pages") or 1)
        rows = res.get("data") or []
        oldest = "9999"
        for r in rows:
            d = str(r.get("STATISTICS_DATE", ""))[:10]
            ratio = _f(r.get("AVG_GUARANTEE_RATIO"))
            if not d or ratio is None:
                continue
            oldest = min(oldest, d)
            if d >= since:
                out.append((d, float(ratio), _f(r.get("TOTAL_GUARANTEE"))))
        if not rows or oldest < since:
            break
        page += 1
    return out


ACCOUNT_FETCH = fetch_account_info          # 自检时替换成离线假数据


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
                # 收盘价（用于"预估买入金额"= Δ份额 × 收盘价；新基金/首次回填 3 年）
                lastp = last_date(conn, "SELECT MAX(date) FROM etf_price WHERE code=?", (c,))
                startp = (datetime.strptime(lastp, "%Y-%m-%d").date() - timedelta(days=OVERLAP_DAYS)) if lastp \
                    else today - timedelta(days=BACKFILL_DAYS)
                try:
                    dp = ts_call(pro.fund_daily, ts_code=c, start_date=_ymd(startp), end_date=_ymd(today))
                    n = 0
                    for d, px in dp[["trade_date", "close"]].values.tolist():
                        if _f(px) is not None:
                            conn.execute("INSERT INTO etf_price(code,date,close) VALUES(?,?,?) "
                                         "ON CONFLICT(code,date) DO UPDATE SET close=excluded.close", (c, _iso(d), float(px)))
                            n += 1
                    log(conn, f"price:{c}", "ok", n, f"from {startp}")
                except Exception as ex:  # noqa: BLE001
                    log(conn, f"price:{c}", "failed", 0, str(ex))
                    print(f"  ⚠ {c} 收盘价取数失败（不影响份额）：{str(ex)[:100]}")

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

        # 沪深300 流通市值（成分股 circ_mv 求和）：失败不影响其它
        try:
            n_snap, n_mv, n_skip, n_need = update_hs300_mv(conn, pro, today)
            log(conn, "hs300_mv", "ok", n_mv, f"snap+{n_snap} skip {n_skip}")
            n_ok += 1
            tot = conn.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM hs300_mv").fetchone()
            print(f"  沪深300流通市值  本次 +{n_mv} 天（待补 {n_need}，跳过 {n_skip}）；库内共 {tot[0]} 天 {tot[1]} ~ {tot[2]}")
        except Exception as ex:  # noqa: BLE001
            n_fail += 1
            log(conn, "hs300_mv", "failed", 0, str(ex))
            print(f"  ⚠ 沪深300流通市值取数失败（不影响其它）：{str(ex)[:160]}")

        # A股每日成交额
        try:
            n_t = update_turnover(conn, pro, today)
            log(conn, "turnover", "ok", n_t)
            n_ok += 1
            t = conn.execute("SELECT COUNT(*),MIN(date),MAX(date) FROM turnover").fetchone()
            lt = conn.execute("SELECT date, sh+sz+COALESCE(bj,0) FROM turnover ORDER BY date DESC LIMIT 1").fetchone()
            print(f"  A股成交额  本次 +{n_t} 天；库内 {t[0]} 天 {t[1]} ~ {t[2]}；最新 {lt[0]} = {lt[1]:.0f} 亿元")
        except Exception as ex:  # noqa: BLE001
            n_fail += 1
            log(conn, "turnover", "failed", 0, str(ex))
            print(f"  ⚠ A股成交额取数失败（不影响其它）：{str(ex)[:160]}")

        # 平均维持担保比例（市场整体）
        last = last_date(conn, "SELECT MAX(date) FROM margin_account")
        start = (datetime.strptime(last, "%Y-%m-%d").date() - timedelta(days=OVERLAP_DAYS)) if last \
            else today - timedelta(days=BACKFILL_DAYS)
        try:
            rows = ACCOUNT_FETCH(start.isoformat())
            for d, ratio, gv in rows:
                conn.execute("INSERT INTO margin_account(date,avg_ratio,guarantee) VALUES(?,?,?) "
                             "ON CONFLICT(date) DO UPDATE SET avg_ratio=excluded.avg_ratio, guarantee=excluded.guarantee",
                             (d, ratio, gv))
            log(conn, "margin_account", "ok", len(rows), f"from {start}")
            n_ok += 1
            print(f"  平均维持担保比例  +{len(rows)} 行" + (f"，最新 {max(rows)[0]} = {max(rows)[1]}" if rows else ""))
        except Exception as ex:  # noqa: BLE001
            n_fail += 1
            log(conn, "margin_account", "failed", 0, str(ex))
            print(f"  ⚠ 平均维持担保比例取数失败（不影响其它）：{str(ex)[:120]}")

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
                 ("159915.SZ", "易方达创业板ETF", "创业板指数收益率"), ("159949.SZ", "华安创业板50ETF", "创业板50指数收益率"),
                 ("159952.SZ", "广发创业板ETF", "创业板指数收益率"), ("159967.SZ", "华夏创业板成长ETF", "创业板成长指数收益率"),
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

    def index_weight(self, index_code, start_date, end_date):
        snaps = sorted({_ymd(d.replace(day=1)) for d in self.days})
        rows = [(index_code, f"S{i}.SH", sn, 1.0) for sn in snaps for i in range(5)]
        return self.pd.DataFrame([r for r in rows if start_date <= r[2] <= end_date] or rows[:0],
                                 columns=["index_code", "con_code", "trade_date", "weight"])

    def daily_basic(self, trade_date, fields=None):
        rows = [(f"S{i}.SH", trade_date, 1000.0 * (i + 1)) for i in range(5)]
        return self.pd.DataFrame(rows, columns=["ts_code", "trade_date", "circ_mv"])

    def index_daily(self, ts_code, start_date, end_date, fields=None):
        if ts_code == "899050.BJ":
            return self.pd.DataFrame([], columns=["ts_code", "trade_date", "close", "amount"])
        k = 4e8 if ts_code.endswith(".SH") else 6e8
        rows = [(ts_code, _ymd(d), 3000.0 + i, k + i * 1e6) for i, d in enumerate(self.days) if start_date <= _ymd(d) <= end_date]
        return self.pd.DataFrame(rows, columns=["ts_code", "trade_date", "close", "amount"])

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
    global ACCOUNT_FETCH
    ACCOUNT_FETCH = lambda since: [(d.isoformat(), 270.0 + i * 0.1, 2.0e13) for i, d in enumerate(pro.days) if d.isoformat() >= since]
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
    cyb = {r[0] for r in conn.execute("SELECT code FROM etf_funds WHERE grp='cyb'")}
    assert cyb == {"159915.SZ", "159952.SZ"}, cyb                                    # 创业板50、成长 被排除
    assert q("SELECT COUNT(*) FROM etf_price") > 500
    assert q("SELECT COUNT(*) FROM margin_account") > 50
    assert q("SELECT COUNT(*) FROM hs300_mv") > 50 and q("SELECT MIN(float_mv) FROM hs300_mv") == 15000.0
    assert q("SELECT COUNT(*) FROM turnover") > 50 and abs(q("SELECT sh FROM turnover ORDER BY date LIMIT 1") - 4000.0) < 1, "turnover"
    n1 = q("SELECT COUNT(*) FROM etf_share"); m1 = q("SELECT COUNT(*) FROM margin")
    assert n1 > 500 and m1 > 100
    conn.close()
    assert run(db, pro) == 0                                                       # 第二次：增量，不重复
    conn = sqlite3.connect(db)
    assert q("SELECT COUNT(*) FROM etf_share") == n1 and q("SELECT COUNT(*) FROM etf_price") > 500 and q("SELECT COUNT(*) FROM margin") == m1
    cfg = json.load(open(CFG_PATH, encoding="utf-8"))
    assert len(cfg["csi500"]) == 6 and cfg["hs300"] == HS300 and len(cfg["sse50"]) == 4 and len(cfg["kc50"]) == 2 and len(cfg["cyb"]) == 2
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
