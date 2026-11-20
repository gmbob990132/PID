#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美股指数 + A 股波动率（QVIX）取数 → data/us.db

指标（metric）与来源；同一指标多个来源时全部尝试、按顺序合并（后面的来源覆盖前面的同日值），
只要有一个来源成功就算成功，任何一个都失败才记失败：
  comp      纳斯达克综合     nasdaq 官网 COMP → 新浪 .IXIC → 东财「纳斯达克」（1991 起）
  ndx       纳斯达克100      nasdaq 官网 NDX  → 新浪 .NDX
  sox       费城半导体       nasdaq 官网 SOX  → 新浪 .SOX
  spx       标普500          新浪 .INX → 东财「标普500」（1986 起）
  vix       VIX 恐慌指数     CBOE 官网 VIX_History.csv（1990 起）
  pe_m      标普500 PE(TTM)  multpl.com 月度表（1871 起；最后一行是"当前估计值"，日期为当天）
  cape_m    席勒 CAPE        multpl.com 月度表（1871 起）
  us10y     美国10年国债     akshare 中美国债收益率（东财）；失败时读 data/macro.db 里宏观模块已有的 us10y
  qvix_csi300 / qvix_cyb / qvix_star50   300ETF / 创业板 / 科创板 期权波动率指数（akshare，2015 起）

akshare 近几个版本频繁改函数名：一律用 akf("新名", "旧名") 取第一个存在的函数。
用法：python3 us_ingest.py           正常取数
      python3 us_ingest.py --selftest 离线自检（不联网）
"""
import io, csv, re, os, sys, json, math, sqlite3
from datetime import date, datetime, timezone
from urllib.request import Request, urlopen

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "us.db")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}

METRICS = {   # id: (名称, 单位)
    "comp": ("纳斯达克综合", "点"), "ndx": ("纳斯达克100", "点"), "sox": ("费城半导体", "点"), "spx": ("标普500", "点"),
    "vix": ("VIX 恐慌指数", ""), "pe_m": ("标普500 PE(TTM)", "倍"), "cape_m": ("席勒 CAPE", "倍"), "us10y": ("美国10年国债收益率", "%"),
    "qvix_csi300": ("沪深300ETF 期权波动率 QVIX", ""), "qvix_cyb": ("创业板 期权波动率 QVIX", ""), "qvix_star50": ("科创板 期权波动率 QVIX", ""),
}


# ---------------- 通用 ----------------
def http_get(url, timeout=60, headers=None):
    return urlopen(Request(url, headers={**UA, **(headers or {})}), timeout=timeout).read().decode("utf-8", "replace")


def num(x):
    try:
        v = float(str(x).replace(",", "").replace("$", "").strip())
        return None if math.isnan(v) or math.isinf(v) else v
    except (TypeError, ValueError):
        return None


_AK = None


def get_ak():
    global _AK
    if _AK is None:
        import akshare as ak
        _AK = ak
    return _AK


def akf(*names):
    ak = get_ak()
    for n in names:
        try:
            fn = getattr(ak, n)
        except Exception:  # noqa: BLE001  新版对已改名的旧名会直接抛异常
            continue
        if callable(fn):
            return fn
    raise RuntimeError("akshare 中找不到：" + " / ".join(names))


def df_rows(df, date_keys=("date", "日期"), val_keys=("close", "收盘", "最新价")):
    cols = list(df.columns)
    dc = next(c for c in cols if any(k == str(c) or k in str(c) for k in date_keys))
    vc = next(c for c in cols if any(k == str(c) or k in str(c) for k in val_keys))
    return [(str(d)[:10], num(v)) for d, v in zip(df[dc], df[vc])]


# ---------------- 各来源 ----------------
def src_nasdaq(sym):
    def f():
        url = (f"https://api.nasdaq.com/api/quote/{sym}/historical?assetclass=index"
               f"&fromdate=2010-01-01&todate={date.today()}&limit=9999")
        j = json.loads(http_get(url, 60, {"Accept": "application/json", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}))
        rows = (((j.get("data") or {}).get("tradesTable") or {}).get("rows")) or []
        out = []
        for r in rows:
            try:
                out.append((datetime.strptime(r["date"], "%m/%d/%Y").date().isoformat(), num(r.get("close"))))
            except (KeyError, ValueError):
                pass
        return out
    f.__name__ = f"nasdaq:{sym}"
    return f


def src_sina(sym):
    def f():
        return df_rows(akf("index_us_daily_sina", "index_us_stock_sina")(symbol=sym))
    f.__name__ = f"sina:{sym}"
    return f


def src_em(name):
    def f():
        return df_rows(akf("index_global_hist_em")(symbol=name), val_keys=("最新价", "收盘"))
    f.__name__ = f"em:{name}"
    return f


def src_cboe_vix():
    txt = http_get("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv", 60)
    out = []
    for r in list(csv.reader(io.StringIO(txt)))[1:]:
        try:
            out.append((datetime.strptime(r[0], "%m/%d/%Y").date().isoformat(), num(r[4])))
        except (ValueError, IndexError):
            pass
    return out
src_cboe_vix.__name__ = "cboe:VIX"


def parse_multpl(h):
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", h, re.S):
        tds = [re.sub(r"<[^>]+>|&#x2002;|&#\d+;|&nbsp;", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(tds) >= 2:
            try:
                d = datetime.strptime(tds[0], "%b %d, %Y").date().isoformat()
            except ValueError:
                continue
            out.append((d, num(re.sub(r"[^\d.\-]", "", tds[1]))))
    return out


def src_multpl(slug):
    def f():
        return parse_multpl(http_get(f"https://www.multpl.com/{slug}/table/by-month", 60))
    f.__name__ = f"multpl:{slug}"
    return f


def src_us10y_ak():
    df = akf("bond_zh_us_yield_em", "bond_zh_us_rate")(start_date="19900101")
    return df_rows(df, val_keys=("美国国债收益率10年",))
src_us10y_ak.__name__ = "akshare:美债10年"


def src_us10y_macro_db():
    p = os.path.join(os.path.dirname(DB), "macro.db")
    c = sqlite3.connect(p)
    try:
        return [(d, v) for d, v in c.execute("SELECT date, value FROM macro_series WHERE key='us10y' ORDER BY date")]
    finally:
        c.close()
src_us10y_macro_db.__name__ = "macro.db:us10y"


def src_qvix(fn_name):
    def f():
        return df_rows(akf(fn_name)())
    f.__name__ = f"akshare:{fn_name}"
    return f


SOURCES = {
    "comp": [src_em("纳斯达克"), src_sina(".IXIC"), src_nasdaq("COMP")],
    "ndx": [src_sina(".NDX"), src_nasdaq("NDX")],
    "sox": [src_sina(".SOX"), src_nasdaq("SOX")],
    "spx": [src_em("标普500"), src_sina(".INX")],
    "vix": [src_cboe_vix],
    "pe_m": [src_multpl("s-p-500-pe-ratio")],
    "cape_m": [src_multpl("shiller-pe")],
    "us10y": [src_us10y_macro_db, src_us10y_ak],          # 宏观库在前，akshare 在后覆盖（更新更及时）
    "qvix_csi300": [src_qvix("index_option_300etf_qvix")],
    "qvix_cyb": [src_qvix("index_option_cyb_qvix")],
    "qvix_star50": [src_qvix("index_option_kcb_qvix")],
}


# ---------------- 库 ----------------
def init_db(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS obs(metric TEXT, date TEXT, value REAL, source TEXT, PRIMARY KEY(metric, date));
    CREATE TABLE IF NOT EXISTS run_log(id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, metric TEXT, status TEXT, message TEXT);
    """)


def valid(d):
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", d or "")) and "1850-01-01" <= d <= date.today().isoformat()


def run(db=DB, sources=None):
    sources = sources or SOURCES
    os.makedirs(os.path.dirname(db), exist_ok=True)
    c = sqlite3.connect(db)
    init_db(c)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n_fail = 0
    for mid, srcs in sources.items():
        merged, used, errs = {}, [], []
        for fn in srcs:
            try:
                rows = [(d, num(v)) for d, v in fn() if num(v) is not None and valid(d)]
                if not rows:
                    raise RuntimeError("0 行")
                for d, v in rows:
                    merged[d] = (v, fn.__name__)
                used.append(f"{fn.__name__}({len(rows)})")
            except Exception as e:  # noqa: BLE001
                errs.append(f"{fn.__name__}: {type(e).__name__} {str(e)[:100]}")
        if not merged:
            n_fail += 1
            msg = "；".join(errs)
            c.execute("INSERT INTO run_log(run_at,metric,status,message) VALUES (?,?,?,?)", (now, mid, "failed", msg))
            print(f"✗ {METRICS[mid][0]}：全部来源失败 → {msg}")
            continue
        before = c.execute("SELECT COUNT(*) FROM obs WHERE metric=?", (mid,)).fetchone()[0]
        c.executemany("INSERT OR REPLACE INTO obs(metric,date,value,source) VALUES (?,?,?,?)",
                      [(mid, d, v, s) for d, (v, s) in merged.items()])
        after = c.execute("SELECT COUNT(*) FROM obs WHERE metric=?", (mid,)).fetchone()[0]
        last = max(merged)
        msg = f"来源 {' + '.join(used)}；最新 {last} = {merged[last][0]}；新增 {after - before} 行" + (f"；部分失败 {errs}" if errs else "")
        c.execute("INSERT INTO run_log(run_at,metric,status,message) VALUES (?,?,?,?)", (now, mid, "partial" if errs else "ok", msg))
        print(f"{'⚠' if errs else '✓'} {METRICS[mid][0]:<12} {msg}")
    c.commit()
    c.close()
    return 1 if n_fail == len(sources) else 0


# ---------------- 自检 ----------------
def selftest():
    import tempfile
    h = ('<table><tr><th>Date</th><th>Value</th></tr><tr><td>Nov 19, 2026</td><td>\n&#x2002;24.46\n</td></tr>'
         '<tr><td>Nov 1, 2026</td><td>25.73</td></tr><tr><td>bad</td><td>1</td></tr></table>')
    assert parse_multpl(h) == [("2026-11-19", 24.46), ("2026-11-01", 25.73)], parse_multpl(h)
    assert num("nan") is None and num("1,234.5") == 1234.5 and num(None) is None
    import types
    def mk(name, rows, boom=False):
        def f():
            if boom:
                raise RuntimeError("down")
            return rows
        f.__name__ = name
        return f
    srcs = {"comp": [mk("a", [("2026-11-18", 1.0), ("2026-11-19", 2.0)]), mk("b", [("2026-11-19", 2.5), ("2026-11-20", float("nan"))])],
            "sox": [mk("x", [], True), mk("y", [("2026-11-19", 9.0), ("9999-01-01", 1.0)])],
            "vix": [mk("z", [], True)]}
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "t.db")
        assert run(db, srcs) == 0
        c = sqlite3.connect(db)
        rows = dict(((m, d), v) for m, d, v in c.execute("SELECT metric,date,value FROM obs"))
        assert rows[("comp", "2026-11-19")] == 2.5 and rows[("comp", "2026-11-18")] == 1.0, rows
        assert ("comp", "2026-11-20") not in rows and ("sox", "9999-01-01") not in rows
        st = dict(c.execute("SELECT metric,status FROM run_log"))
        assert st == {"comp": "ok", "sox": "partial", "vix": "failed"}, st
        assert run(db, {"vix": [mk("z", [], True)]}) == 1
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    sys.exit(run())
