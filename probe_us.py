#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美股指数数据源探测（只读，不写库、不提交）。
目标：纳斯达克综合 / 纳斯达克100 / 费城半导体 SOX 的日度点位（尽量长历史），
      顺带：标普500 PE、席勒 CAPE、VIX。
每个来源独立 try，失败只打印原因。用法：python3 probe_us.py
"""
import os, re, io, csv, json, time, traceback
from datetime import date, datetime
from urllib.request import Request, urlopen

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}


def get(url, timeout=40, headers=None):
    return urlopen(Request(url, headers={**UA, **(headers or {})}), timeout=timeout).read().decode("utf-8", "replace")


def section(t):
    print("\n" + "=" * 72 + f"\n{t}\n" + "=" * 72, flush=True)


def report(name, rows):
    """rows: [(日期str, 值)] 按日期排序后打印概况"""
    rows = sorted((d, v) for d, v in rows if v is not None)
    if not rows:
        print(f"  ✗ {name}: 0 行"); return
    print(f"  ✓ {name}: {len(rows)} 行  {rows[0][0]} ~ {rows[-1][0]}  最新 {rows[-1][1]}  前两条 {rows[-3:-1]}")


def tryit(name, fn):
    try:
        report(name, fn())
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {name}: {type(e).__name__} {str(e)[:180]}")


def num(x):
    try:
        return float(str(x).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


# ---------- A. akshare 新浪美股指数 ----------
def part_a():
    section("A. akshare 新浪美股指数（index_us_daily_sina / 旧名 index_us_stock_sina）")
    import akshare as ak
    print("akshare", ak.__version__)
    fn = None
    for n in ("index_us_daily_sina", "index_us_stock_sina"):
        try:
            fn = getattr(ak, n); print("用", n); break
        except Exception:  # noqa: BLE001
            pass
    for sym in (".IXIC", ".NDX", ".SOX", ".INX", ".DJI", ".VIX", "SOX", ".SPX"):
        def f(sym=sym):
            df = fn(symbol=sym)
            return [(str(d)[:10], num(c)) for d, c in zip(df["date"], df["close"])]
        tryit(f"sina {sym}", f)
    # 看看 akshare 有没有别的美股指数接口
    print("  akshare 里含 index_us / global 的函数：", [n for n in dir(ak) if re.search(r"index_(us|global)|_us_index", n)])
    for n in ("index_global_hist_em",):
        if hasattr(ak, n):
            for sym in ("纳斯达克", "费城半导体", "标普500", "纳斯达克100"):
                def g(n=n, sym=sym):
                    df = getattr(ak, n)(symbol=sym)
                    dc = [c for c in df.columns if "日期" in c][0]; cc = [c for c in df.columns if "最新" in c or "收盘" in c][0]
                    return [(str(d)[:10], num(v)) for d, v in zip(df[dc], df[cc])]
                tryit(f"{n}({sym})", g)


# ---------- B. FRED（无 key 的 CSV 下载）----------
def part_b():
    section("B. FRED fredgraph.csv（免 key）")
    for sid in ("NASDAQCOM", "NASDAQ100", "SP500", "VIXCLS", "DGS10"):
        def f(sid=sid):
            txt = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}", 60)
            r = list(csv.reader(io.StringIO(txt)))
            return [(a[0], num(a[1])) for a in r[1:] if len(a) > 1]
        tryit(f"FRED {sid}", f)


# ---------- C. Nasdaq 官方 API ----------
def part_c():
    section("C. api.nasdaq.com 指数历史（COMP / NDX / SOX）")
    for sym in ("COMP", "NDX", "SOX"):
        def f(sym=sym):
            url = (f"https://api.nasdaq.com/api/quote/{sym}/historical?assetclass=index"
                   f"&fromdate=2010-01-01&todate={date.today()}&limit=9999")
            j = json.loads(get(url, 60, {"Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}))
            rows = (((j.get("data") or {}).get("tradesTable") or {}).get("rows")) or []
            out = []
            for r in rows:
                try:
                    d = datetime.strptime(r["date"], "%m/%d/%Y").date().isoformat()
                except (KeyError, ValueError):
                    continue
                out.append((d, num(r.get("close"))))
            if not out:
                print("    返回片段：", json.dumps(j)[:200])
            return out
        tryit(f"nasdaq {sym}", f)


# ---------- D. Tushare index_global ----------
def part_d():
    section("D. Tushare index_global（需要 TUSHARE_TOKEN）")
    tok = os.environ.get("TUSHARE_TOKEN")
    if not tok:
        print("  未设置 TUSHARE_TOKEN，跳过"); return
    import tushare as ts
    pro = ts.pro_api(tok)
    for code in ("IXIC", "NDX", "SOX", "SPX", "DJI", "VIX"):
        def f(code=code):
            df = pro.index_global(ts_code=code, start_date="20100101")
            return [(f"{d[:4]}-{d[4:6]}-{d[6:]}", num(c)) for d, c in zip(df["trade_date"], df["close"])]
        tryit(f"tushare {code}", f)


# ---------- E. stooq / yahoo ----------
def part_e():
    section("E. stooq CSV / Yahoo chart")
    for s in ("^ndx", "^ndq", "^sox", "^spx"):
        def f(s=s):
            txt = get(f"https://stooq.com/q/d/l/?s={s.replace('^', '%5E')}&i=d", 40)
            r = list(csv.reader(io.StringIO(txt)))
            if len(r) < 3:
                print("    stooq 返回：", txt[:120].replace("\n", " "))
            return [(a[0], num(a[4])) for a in r[1:] if len(a) > 4]
        tryit(f"stooq {s}", f)
    for s in ("^SOX", "^NDX", "^IXIC"):
        def g(s=s):
            j = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{s.replace('^', '%5E')}?range=max&interval=1d", 40))
            r = j["chart"]["result"][0]
            return [(date.fromtimestamp(t).isoformat(), c) for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"])]
        tryit(f"yahoo {s}", g)
        time.sleep(3)


# ---------- F. 估值：multpl ----------
def part_f():
    section("F. 估值：multpl.com 标普500 PE / 席勒 CAPE（月度表）")
    for slug in ("s-p-500-pe-ratio", "shiller-pe", "s-p-500-earnings-yield"):
        def f(slug=slug):
            h = get(f"https://www.multpl.com/{slug}/table/by-month", 60)
            out = []
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", h, re.S):
                tds = [re.sub(r"<[^>]+>|&#x2002;|&#\d+;", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
                if len(tds) >= 2:
                    try:
                        d = datetime.strptime(tds[0], "%b %d, %Y").date().isoformat()
                    except ValueError:
                        continue
                    out.append((d, num(re.sub(r"[^\d.\-]", "", tds[1]))))
            return out
        tryit(f"multpl {slug}", f)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        assert num("1,234.5") == 1234.5 and num("$17,000.12") == 17000.12 and num("x") is None
        report("t", [("2026-01-02", 2.0), ("2026-01-01", 1.0), ("2026-01-03", 3.0)])
        print("selftest OK"); sys.exit(0)
    for p in (part_a, part_b, part_c, part_d, part_e, part_f):
        try:
            p()
        except Exception:  # noqa: BLE001
            print("本节异常：", traceback.format_exc()[-800:])
    print("\n探测结束")
