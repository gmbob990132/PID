#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技术指标分析 · MACD 模块 · 落库脚本（独立一套，不依赖其他模块）

做两件事（业务逻辑移植自本地 stock_alert 程序）：
  1) 零轴金叉/死叉：DIFF 上穿/下穿 DEA。所有交叉都落库（含日线、周线），
     "零轴附近"（|DIFF|/收盘价 < 阈值）只是显示层的筛选，阈值可在页面调。
  2) 6 态状态机：按 DIFF/DEA 相对零轴与彼此的位置，每根 K 线独立判定状态
     弱 / 中性偏强 / 极强 / 强 / 中性偏弱 / 极弱；状态变化即"切换事件"。

数据源：
  A 股 (.SH/.SZ/.BJ) : Tushare pro_bar 前复权（需环境变量 TUSHARE_TOKEN；有 50 次/分钟限速，内置限速+重试）
  港股 (.HK)         : 腾讯财经 fqkline 前复权
股票池：tech_config/stocks.csv（页面"股票池"会改它）→ 同步进 stocks 表；删除只是停用，历史保留。

用法：
  python3 tech_ingest.py                 # 联网抓取 → data/tech.db
  python3 tech_ingest.py --demo          # 用合成数据跑通全流程 → data/tech_demo.db（不联网）
  python3 tech_ingest.py --selftest      # 离线自检
  python3 tech_ingest.py --only 600031.SH,00700.HK   # 只处理指定代码（调试）

表：
  stocks(code,name,market,focus,enabled,added_at,removed_at)
  macd_daily(code,date,close,pct_chg,diff,dea,hist,state,prev_state,trigger)  PK(code,date)
  macd_cross(code,period,kline_date,kind,close,diff,dea,ratio)               PK(code,period,kline_date,kind)
  run_log(id,run_date,started_at,finished_at,n_total,n_ok,n_fail,detail)
"""
import os
import re
import sys
import csv
import json
import time
import sqlite3
import argparse
import threading
from collections import deque
from datetime import datetime, date, timedelta, timezone
from urllib.request import Request, urlopen

import pandas as pd

CN_TZ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "tech.db")
DEMO_DB_PATH = os.path.join(HERE, "data", "tech_demo.db")
STOCKS_CSV = os.path.join(HERE, "tech_config", "stocks.csv")

MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
LOOKBACK_DAYS = 730        # 拉取的自然日；日线 ~500 根，周线 ~100 根，足够 EMA 收敛
DAILY_WARMUP = 60          # 前 N 根日线只用于 EMA 预热，不落库
WEEKLY_WARMUP = 40         # 前 N 根周线同理
MIN_BARS = DAILY_WARMUP + 5

CODE_RE = re.compile(r"^(\d{6}\.(SH|SZ|BJ)|\d{5}\.HK)$")
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# ============================================================
# 6 态状态机（逐字移植自 stock_alert/strategies/macd_state/macd_state.py）
# ============================================================
STATE_WEAK = "弱"
STATE_NEUTRAL_STRONG = "中性偏强"
STATE_VERY_STRONG = "极强"
STATE_STRONG = "强"
STATE_NEUTRAL_WEAK = "中性偏弱"
STATE_VERY_WEAK = "极弱"
STATE_RING = [STATE_WEAK, STATE_NEUTRAL_STRONG, STATE_VERY_STRONG,
              STATE_STRONG, STATE_NEUTRAL_WEAK, STATE_VERY_WEAK]


def classify_state(diff, dea):
    """用 DIFF/DEA 快照直接判定状态，每根 K 线独立，不依赖历史。"""
    if diff < 0 and dea < 0:
        return STATE_NEUTRAL_STRONG if diff >= dea else STATE_WEAK
    if diff > 0 and dea > 0:
        return STATE_STRONG if diff >= dea else STATE_NEUTRAL_WEAK
    if diff > 0 and dea < 0:
        return STATE_VERY_STRONG
    if diff < 0 and dea > 0:
        return STATE_VERY_WEAK
    # 边界：其中一个恰好 == 0
    if diff >= dea:
        return STATE_STRONG if diff > 0 else STATE_NEUTRAL_STRONG
    else:
        return STATE_NEUTRAL_WEAK if diff > 0 else STATE_WEAK


def detect_events(diff_now, dea_now, diff_prev, dea_prev):
    """从 prev 到 now 这一根 K 线发生的事件（用于描述"为什么切换"）。"""
    events = []
    if diff_prev <= 0 and diff_now > 0:
        events.append("DIF上穿零轴")
    elif diff_prev >= 0 and diff_now < 0:
        events.append("DIF下穿零轴")
    if dea_prev <= 0 and dea_now > 0:
        events.append("DEA上穿零轴")
    elif dea_prev >= 0 and dea_now < 0:
        events.append("DEA下穿零轴")
    crossed_up = diff_prev <= dea_prev and diff_now > dea_now
    crossed_dn = diff_prev >= dea_prev and diff_now < dea_now
    if crossed_up:
        events.append("低位金叉" if (diff_now < 0 and dea_now < 0) else "高位金叉")
    elif crossed_dn:
        events.append("高位死叉" if (diff_now > 0 and dea_now > 0) else "低位死叉")
    return events


def calc_macd(close):
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    diff = ema_fast - ema_slow
    dea = diff.ewm(span=MACD_SIGNAL, adjust=False).mean()
    return diff, dea


def compute_series(df):
    """df: date, close(+其他列) 升序。返回加了 diff/dea/hist/pct_chg/state/prev_state/trigger 的新表。"""
    df = df.reset_index(drop=True).copy()
    diff, dea = calc_macd(df["close"])
    df["diff"], df["dea"] = diff, dea
    df["hist"] = (diff - dea) * 2
    df["pct_chg"] = df["close"].pct_change() * 100

    n = len(df)
    dv, ev = df["diff"].to_numpy(), df["dea"].to_numpy()
    states = [""] * n
    for i in range(n):
        if pd.isna(dv[i]) or pd.isna(ev[i]):
            states[i] = states[i - 1] if i > 0 and states[i - 1] else STATE_WEAK
        else:
            states[i] = classify_state(float(dv[i]), float(ev[i]))
    prev_states, triggers = [""] * n, [""] * n
    for i in range(1, n):
        if states[i] == states[i - 1]:
            continue
        prev_states[i] = states[i - 1]
        if any(pd.isna(x) for x in (dv[i], ev[i], dv[i - 1], ev[i - 1])):
            triggers[i] = "(状态变化)"
        else:
            evs = detect_events(float(dv[i]), float(ev[i]), float(dv[i - 1]), float(ev[i - 1]))
            triggers[i] = "+".join(evs) if evs else "(状态变化)"
    df["state"], df["prev_state"], df["trigger"] = states, prev_states, triggers
    return df


def find_crosses(df, start_idx):
    """返回 [(kline_date, kind, close, diff, dea, ratio)]，只含 index>=start_idx 的交叉。
    金叉：当根 DIFF>DEA 且上一根 DIFF<=DEA；死叉：当根 DIFF<DEA 且上一根 DIFF>=DEA。"""
    pd_, pe_ = df["diff"].shift(), df["dea"].shift()
    golden = (df["diff"] > df["dea"]) & (pd_ <= pe_)
    death = (df["diff"] < df["dea"]) & (pd_ >= pe_)
    out = []
    for i in range(max(start_idx, 1), len(df)):
        kind = "golden" if golden.iloc[i] else ("death" if death.iloc[i] else None)
        if kind is None:
            continue
        close = float(df["close"].iloc[i])
        if close <= 0:
            continue
        out.append((df["date"].iloc[i], kind, close, float(df["diff"].iloc[i]),
                    float(df["dea"].iloc[i]), abs(float(df["diff"].iloc[i])) / close))
    return out


def to_weekly_complete(df_daily):
    """日线 → 周线（W-FRI），只保留已收完的周。
    规则：周标签（周五）> 最新日线日期，说明这周还没走完，丢弃；
    日期取该周最后一个交易日（遇到节假日周五休市时更真实）。"""
    d = df_daily.copy()
    d["last_day"] = d["date"]
    w = d.set_index("date").resample("W-FRI").agg({"close": "last", "last_day": "last"}).dropna()
    latest = d["date"].iloc[-1]
    w = w[w.index <= latest]
    w = w.reset_index(drop=True).rename(columns={"last_day": "date"})
    return w[["date", "close"]]


# ============================================================
# Tushare 限速 + 超限重试（内置一份，保持模块独立）
# ============================================================
_TS_MAX = int(os.environ.get("TUSHARE_MAX_CALLS_PER_MIN", "45"))
_ts_lock = threading.Lock()
_ts_calls = deque()


def _throttle():
    while True:
        with _ts_lock:
            now = time.monotonic()
            while _ts_calls and now - _ts_calls[0] >= 60.0:
                _ts_calls.popleft()
            if len(_ts_calls) < _TS_MAX:
                _ts_calls.append(now)
                return
            wait = 60.0 - (now - _ts_calls[0]) + 0.2
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
            with _ts_lock:
                _ts_calls.clear()


# ============================================================
# 数据获取
# ============================================================
def fetch_a(code, start, end):
    import tushare as ts
    token = os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        raise RuntimeError("环境变量 TUSHARE_TOKEN 未设置")
    ts.set_token(token)
    df = ts_call(ts.pro_bar, ts_code=code, adj="qfq", start_date=start, end_date=end, asset="E", freq="D")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"trade_date": "date"})
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    return df[["date", "close"]].sort_values("date").reset_index(drop=True)


def fetch_hk(code, start, end):
    bare = code.split(".")[0].lstrip("0").zfill(5)
    sd = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
    ed = f"{end[:4]}-{end[4:6]}-{end[6:8]}"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=hk{bare},day,{sd},{ed},800,qfq"
    req = Request(url, headers={"User-Agent": _UA})
    with urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8", errors="replace"))
    inner = (data.get("data") or {}).get(f"hk{bare}") or {}
    kline = inner.get("qfqday") or inner.get("day") or []
    rows = []
    for row in kline:
        if len(row) < 3:
            continue
        try:
            rows.append({"date": pd.to_datetime(row[0]), "close": float(row[2])})  # [date, open, close, ...]
        except (ValueError, TypeError):
            continue
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def fetch_demo(code, start, end):
    """合成数据：按代码做种子的随机游走，只在工作日有 K 线；供离线开发/演示。"""
    import random
    rnd = random.Random(code)
    days = pd.bdate_range(pd.Timestamp(start), pd.Timestamp(end))
    px, rows = rnd.uniform(5, 80), []
    drift = rnd.uniform(-0.0004, 0.0008)
    for d in days:
        px *= 1 + rnd.gauss(drift, 0.02)
        rows.append({"date": d, "close": round(max(px, 0.5), 3)})
    return pd.DataFrame(rows)


def fetch_bars(code, start, end, demo=False):
    if demo:
        return fetch_demo(code, start, end)
    if code.endswith(".HK"):
        return fetch_hk(code, start, end)
    return fetch_a(code, start, end)


def resolve_names(codes, demo=False):
    """给没有名称的代码补名称。失败不致命，返回能补到的部分。"""
    names = {}
    if demo or not codes:
        return {c: c for c in codes} if demo else names
    a_codes = [c for c in codes if not c.endswith(".HK")]
    if a_codes:
        try:
            import tushare as ts
            ts.set_token(os.environ.get("TUSHARE_TOKEN", ""))
            pro = ts.pro_api()
            b = ts_call(pro.stock_basic, exchange="", list_status="L", fields="ts_code,name")
            m = dict(zip(b["ts_code"], b["name"]))
            names.update({c: m[c] for c in a_codes if c in m})
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 补全 A 股名称失败: {str(e)[:120]}")
    for c in [c for c in codes if c.endswith(".HK")]:
        try:
            bare = c.split(".")[0].lstrip("0").zfill(5)
            with urlopen(Request(f"https://qt.gtimg.cn/q=hk{bare}", headers={"User-Agent": _UA}), timeout=10) as r:
                txt = r.read().decode("gbk", errors="replace")
            parts = txt.split('"')[1].split("~")
            if len(parts) > 1 and parts[1]:
                names[c] = parts[1]
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 补全 {c} 名称失败: {str(e)[:80]}")
    return names


# ============================================================
# 数据库
# ============================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks(
  code TEXT PRIMARY KEY, name TEXT, market TEXT, focus INTEGER DEFAULT 0,
  enabled INTEGER DEFAULT 1, added_at TEXT, removed_at TEXT);
CREATE TABLE IF NOT EXISTS macd_daily(
  code TEXT NOT NULL, date TEXT NOT NULL,
  close REAL, pct_chg REAL, diff REAL, dea REAL, hist REAL,
  state TEXT, prev_state TEXT, trigger TEXT,
  PRIMARY KEY(code, date));
CREATE INDEX IF NOT EXISTS idx_daily_date ON macd_daily(date);
CREATE TABLE IF NOT EXISTS macd_cross(
  code TEXT NOT NULL, period TEXT NOT NULL, kline_date TEXT NOT NULL, kind TEXT NOT NULL,
  close REAL, diff REAL, dea REAL, ratio REAL,
  PRIMARY KEY(code, period, kline_date, kind));
CREATE INDEX IF NOT EXISTS idx_cross_date ON macd_cross(kline_date);
CREATE TABLE IF NOT EXISTS run_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_date TEXT, started_at TEXT, finished_at TEXT,
  n_total INTEGER, n_ok INTEGER, n_fail INTEGER, detail TEXT);
"""


def open_db(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def read_stock_csv(path):
    """读股票池 CSV。非法代码跳过并报告；重复代码保留首个。返回 (rows, problems)。"""
    rows, seen, problems = [], set(), []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for ln, r in enumerate(csv.DictReader(f), start=2):
            code = (r.get("code") or "").strip().upper()
            if not code:
                continue
            if not CODE_RE.match(code):
                problems.append(f"第{ln}行 代码格式不对: {code!r}")
                continue
            if code in seen:
                problems.append(f"第{ln}行 重复代码已忽略: {code}")
                continue
            seen.add(code)
            focus = 1 if str(r.get("focus", "")).strip() in ("1", "是", "true", "True", "Y", "y") else 0
            rows.append({"code": code, "name": (r.get("name") or "").strip(), "focus": focus})
    return rows, problems


def sync_stocks(conn, csv_path, today, demo=False):
    rows, problems = read_stock_csv(csv_path)
    for p in problems:
        print("  ⚠️", p)
    if not rows:
        raise RuntimeError(f"{csv_path} 里没有有效股票，已中止（避免把全部股票停用）")
    in_csv = {r["code"] for r in rows}
    cur = {r[0]: r for r in conn.execute("SELECT code,name,enabled FROM stocks")}
    for r in rows:
        mkt = "HK" if r["code"].endswith(".HK") else "A"
        if r["code"] in cur:
            name = r["name"] or cur[r["code"]][1] or ""
            conn.execute("UPDATE stocks SET name=?,focus=?,enabled=1,removed_at=NULL,market=? WHERE code=?",
                         (name, r["focus"], mkt, r["code"]))
        else:
            conn.execute("INSERT INTO stocks(code,name,market,focus,enabled,added_at) VALUES(?,?,?,?,1,?)",
                         (r["code"], r["name"], mkt, r["focus"], today))
    for code, (_, _, enabled) in cur.items():
        if code not in in_csv and enabled:
            conn.execute("UPDATE stocks SET enabled=0, removed_at=? WHERE code=?", (today, code))
    conn.commit()
    missing = [c for c, n in conn.execute("SELECT code,name FROM stocks WHERE enabled=1") if not n]
    if missing:
        got = resolve_names(missing, demo=demo)
        for c, n in got.items():
            conn.execute("UPDATE stocks SET name=? WHERE code=?", (n, c))
        conn.commit()
    return len(rows)


_R = lambda x, n: None if x is None or pd.isna(x) else round(float(x), n)  # noqa: E731


def store_stock(conn, code, df, weekly_df):
    """把一只股票的计算结果落库（幂等；值没变就不改行，减少 git 里 db 文件的变动）。"""
    d = df.iloc[DAILY_WARMUP:]
    rows = [(code, r.date.strftime("%Y-%m-%d"), _R(r.close, 3), _R(r.pct_chg, 2), _R(r.diff, 4),
             _R(r.dea, 4), _R(r.hist, 4), r.state, r.prev_state, r.trigger) for r in d.itertuples()]
    conn.executemany("""
        INSERT INTO macd_daily(code,date,close,pct_chg,diff,dea,hist,state,prev_state,trigger)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(code,date) DO UPDATE SET close=excluded.close,pct_chg=excluded.pct_chg,
          diff=excluded.diff,dea=excluded.dea,hist=excluded.hist,state=excluded.state,
          prev_state=excluded.prev_state,trigger=excluded.trigger
        WHERE close IS NOT excluded.close OR pct_chg IS NOT excluded.pct_chg OR diff IS NOT excluded.diff
           OR dea IS NOT excluded.dea OR hist IS NOT excluded.hist OR state IS NOT excluded.state
           OR prev_state IS NOT excluded.prev_state OR trigger IS NOT excluded.trigger""", rows)

    first_daily = d["date"].iloc[0].strftime("%Y-%m-%d")
    cross_rows = [(code, "D", c[0].strftime("%Y-%m-%d"), c[1], _R(c[2], 3), _R(c[3], 4), _R(c[4], 4), round(c[5], 6))
                  for c in find_crosses(df, DAILY_WARMUP)]
    first_weekly = None
    if weekly_df is not None and len(weekly_df) > WEEKLY_WARMUP + 2:
        w = weekly_df.copy()
        w["diff"], w["dea"] = calc_macd(w["close"])
        wc = find_crosses(w, WEEKLY_WARMUP)
        first_weekly = w["date"].iloc[WEEKLY_WARMUP].strftime("%Y-%m-%d")
        cross_rows += [(code, "W", c[0].strftime("%Y-%m-%d"), c[1], _R(c[2], 3), _R(c[3], 4), _R(c[4], 4), round(c[5], 6))
                       for c in wc]
    # 清掉窗口内已不存在的旧交叉（例如前复权重估后不再成立），再 upsert 新的
    for period, lo in (("D", first_daily), ("W", first_weekly)):
        if lo is None:
            continue
        keep = {(r[1], r[2], r[3]) for r in cross_rows if r[1] == period}
        for kd, kind in conn.execute("SELECT kline_date,kind FROM macd_cross WHERE code=? AND period=? AND kline_date>=?",
                                     (code, period, lo)).fetchall():
            if (period, kd, kind) not in keep:
                conn.execute("DELETE FROM macd_cross WHERE code=? AND period=? AND kline_date=? AND kind=?",
                             (code, period, kd, kind))
    conn.executemany("""
        INSERT INTO macd_cross(code,period,kline_date,kind,close,diff,dea,ratio) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(code,period,kline_date,kind) DO UPDATE SET close=excluded.close,diff=excluded.diff,
          dea=excluded.dea,ratio=excluded.ratio
        WHERE close IS NOT excluded.close OR diff IS NOT excluded.diff OR dea IS NOT excluded.dea
           OR ratio IS NOT excluded.ratio""", cross_rows)
    return len(rows), len(cross_rows)


def process_stock(conn, code, demo=False, today=None):
    end = (today or datetime.now(CN_TZ).date())
    start = end - timedelta(days=LOOKBACK_DAYS)
    df = fetch_bars(code, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), demo=demo)
    if df.empty:
        raise RuntimeError("无数据")
    if len(df) < MIN_BARS:
        raise RuntimeError(f"数据不足 ({len(df)} 根 < {MIN_BARS})")
    full = compute_series(df)
    weekly = to_weekly_complete(df)
    n_rows, n_cross = store_stock(conn, code, full, weekly)
    return full["date"].iloc[-1].strftime("%Y-%m-%d"), n_rows, n_cross


# ============================================================
# 主流程
# ============================================================
def run(db_path, csv_path, demo=False, only=None):
    started = datetime.now(CN_TZ)
    today = started.date()
    conn = open_db(db_path)
    n = sync_stocks(conn, csv_path, today.isoformat(), demo=demo)
    codes = [r[0] for r in conn.execute("SELECT code FROM stocks WHERE enabled=1 ORDER BY market DESC, code")]
    if only:
        codes = [c for c in codes if c in only]
    print(f"股票池 CSV {n} 只；启用 {len(codes)} 只；数据库 {db_path}")
    if not demo and any(not c.endswith(".HK") for c in codes) and not os.environ.get("TUSHARE_TOKEN"):
        print("❌ 有 A 股但 TUSHARE_TOKEN 未设置")
        sys.exit(1)

    ok, fail = 0, {}
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        try:
            last, nr, nc = process_stock(conn, code, demo=demo, today=today)
            conn.commit()
            ok += 1
            if i % 20 == 0 or i == len(codes):
                print(f"  [{i:>4}/{len(codes)}] {code} 最新K线 {last} (累计成功 {ok}, 失败 {len(fail)}, {time.time() - t0:.0f}s)", flush=True)
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            fail[code] = str(e)[:160]
            print(f"  [{i:>4}/{len(codes)}] {code} ❌ {fail[code]}", flush=True)

    # "数据不足"（新股上市不久，K 线不够预热 MACD）不是故障，单独归类，不计入失败
    short = {c: m for c, m in fail.items() if m.startswith("数据不足")}
    n_fail = len(fail) - len(short)
    conn.execute("INSERT INTO run_log(run_date,started_at,finished_at,n_total,n_ok,n_fail,detail) VALUES(?,?,?,?,?,?,?)",
                 (today.isoformat(), started.isoformat(timespec="seconds"),
                  datetime.now(CN_TZ).isoformat(timespec="seconds"), len(codes), ok, n_fail,
                  json.dumps(fail, ensure_ascii=False)))
    conn.commit()
    conn.close()
    print(f"\n完成：成功 {ok} / 失败 {n_fail} / 新股数据不足 {len(short)} / 共 {len(codes)}，用时 {time.time() - t0:.0f}s")
    if short:
        print("数据不足(新股，积累够 K 线后自动加入):", json.dumps(short, ensure_ascii=False)[:800])
    if n_fail:
        print("失败清单:", json.dumps({c: m for c, m in fail.items() if c not in short}, ensure_ascii=False)[:1500])
    if codes and ok == 0:
        sys.exit(1)


def selftest():
    # 1) 状态分类：6 个区域 + 边界
    cases = [((-1, -2), STATE_NEUTRAL_STRONG), ((-2, -1), STATE_WEAK), ((2, 1), STATE_STRONG),
             ((1, 2), STATE_NEUTRAL_WEAK), ((1, -1), STATE_VERY_STRONG), ((-1, 1), STATE_VERY_WEAK),
             ((0, 0), STATE_NEUTRAL_STRONG)]
    for (d, e), want in cases:
        assert classify_state(d, e) == want, (d, e, want)
    # 2) 事件
    assert detect_events(-0.1, -0.2, -0.3, -0.2) == ["低位金叉"]
    assert detect_events(0.1, -0.2, -0.1, -0.2) == ["DIF上穿零轴"]
    assert detect_events(0.3, 0.2, 0.1, 0.2) == ["高位金叉"]
    assert detect_events(0.1, 0.2, 0.3, 0.2) == ["高位死叉"]
    # 3) 合成数据全链路 + 幂等
    import tempfile
    tmp = tempfile.mkdtemp()
    csvp = os.path.join(tmp, "s.csv")
    with open(csvp, "w", encoding="utf-8-sig") as f:
        f.write("code,name,focus\n600031.SH,三一重工,1\n00700.HK,,0\nBAD,x,0\n600031.SH,dup,0\n")
    dbp = os.path.join(tmp, "t.db")
    run(dbp, csvp, demo=True)
    conn = sqlite3.connect(dbp)
    n1 = conn.execute("SELECT COUNT(*) FROM macd_daily").fetchone()[0]
    c1 = conn.execute("SELECT COUNT(*) FROM macd_cross").fetchone()[0]
    assert n1 > 300 and c1 > 5, (n1, c1)
    assert conn.execute("SELECT COUNT(*) FROM stocks WHERE enabled=1").fetchone()[0] == 2
    snap = conn.execute("SELECT * FROM macd_daily ORDER BY code,date").fetchall()
    conn.close()
    run(dbp, csvp, demo=True)  # 第二次：数据不变
    conn = sqlite3.connect(dbp)
    assert conn.execute("SELECT COUNT(*) FROM macd_daily").fetchone()[0] == n1
    assert conn.execute("SELECT * FROM macd_daily ORDER BY code,date").fetchall() == snap
    # 4) 删除股票 → 停用但保留历史
    with open(csvp, "w", encoding="utf-8-sig") as f:
        f.write("code,name,focus\n600031.SH,三一重工,1\n")
    conn.close()
    run(dbp, csvp, demo=True)
    conn = sqlite3.connect(dbp)
    assert conn.execute("SELECT enabled FROM stocks WHERE code='00700.HK'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM macd_daily WHERE code='00700.HK'").fetchone()[0] > 300
    # 5) 周线只含已收完的周
    d = pd.DataFrame({"date": pd.bdate_range("2026-09-01", "2026-10-07"), "close": 1.0})  # 最新为周三
    w = to_weekly_complete(d)
    assert w["date"].iloc[-1] <= pd.Timestamp("2026-10-02"), w.tail()
    print("selftest OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--csv", default=STOCKS_CSV)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    db = a.db or (DEMO_DB_PATH if a.demo else DB_PATH)
    run(db, a.csv, demo=a.demo, only=set(filter(None, a.only.split(","))) or None)


if __name__ == "__main__":
    main()
