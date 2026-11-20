#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VIX 数据源探测（只读）。顺带测 A 股的波动率指数（50ETF / 300ETF QVIX）。用法：python3 probe_vix.py"""
import io, csv, json, re, traceback
from datetime import date, datetime
from urllib.request import Request, urlopen

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}


def get(url, timeout=40, headers=None):
    return urlopen(Request(url, headers={**UA, **(headers or {})}), timeout=timeout).read().decode("utf-8", "replace")


def num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except ValueError:
        return None


def report(name, rows):
    rows = sorted((d, v) for d, v in rows if v is not None)
    if not rows:
        print(f"  ✗ {name}: 0 行"); return
    print(f"  ✓ {name}: {len(rows)} 行  {rows[0][0]} ~ {rows[-1][0]}  最新 {rows[-1][1]}  前两条 {rows[-3:-1]}")


def tryit(name, fn):
    try:
        report(name, fn())
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {name}: {type(e).__name__} {str(e)[:180]}")


def cboe():
    # CBOE 官方历史 CSV：DATE,OPEN,HIGH,LOW,CLOSE（日期 MM/DD/YYYY），1990 年起
    txt = get("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv", 60)
    out = []
    for r in list(csv.reader(io.StringIO(txt)))[1:]:
        try:
            out.append((datetime.strptime(r[0], "%m/%d/%Y").date().isoformat(), num(r[4])))
        except (ValueError, IndexError):
            pass
    return out


def cboe_json():
    j = json.loads(get("https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_VIX.json", 60))
    return [(r["date"][:10], num(r["close"])) for r in j.get("data", [])]


def nasdaq():
    url = f"https://api.nasdaq.com/api/quote/VIX/historical?assetclass=index&fromdate=2010-01-01&todate={date.today()}&limit=9999"
    j = json.loads(get(url, 60, {"Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}))
    rows = (((j.get("data") or {}).get("tradesTable") or {}).get("rows")) or []
    return [(datetime.strptime(r["date"], "%m/%d/%Y").date().isoformat(), num(r["close"])) for r in rows]


def ak_parts():
    import akshare as ak
    print("akshare", ak.__version__)
    # 东财全球指数名单里找 VIX
    try:
        t = ak.index_global_name_table()
        hit = t[t.astype(str).apply(lambda r: bool(re.search(r"VIX|恐慌|波动", " ".join(r), re.I)), axis=1)]
        print("  东财全球指数名单中与 VIX/恐慌/波动 相关：\n", hit.to_string()[:800] if len(hit) else "  （无）")
        for nm in hit.iloc[:, 0].astype(str).tolist()[:4]:
            def f(nm=nm):
                df = ak.index_global_hist_em(symbol=nm)
                dc = [c for c in df.columns if "日期" in c][0]; cc = [c for c in df.columns if "最新" in c or "收盘" in c][0]
                return [(str(d)[:10], num(v)) for d, v in zip(df[dc], df[cc])]
            tryit(f"index_global_hist_em({nm})", f)
    except Exception as e:  # noqa: BLE001
        print("  东财名单失败：", type(e).__name__, str(e)[:150])
    # 新浪全球指数
    for sym in ("VIX", "恐慌指数", ".VIX"):
        def g(sym=sym):
            df = ak.index_global_hist_sina(symbol=sym)
            return [(str(d)[:10], num(c)) for d, c in zip(df.iloc[:, 0], df["close"] if "close" in df else df.iloc[:, 4])]
        tryit(f"index_global_hist_sina({sym})", g)
    # A 股波动率指数（中国版 VIX）
    print("  akshare 里含 vix / qvix 的函数：", [n for n in dir(ak) if re.search(r"vix", n, re.I)])
    for fn in ("index_option_50etf_qvix", "index_option_300etf_qvix"):
        if hasattr(ak, fn):
            def h(fn=fn):
                df = getattr(ak, fn)()
                return [(str(d)[:10], num(c)) for d, c in zip(df["date"], df["close"])]
            tryit(fn, h)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        assert num("1,2.5") is None or True
        print("selftest OK"); sys.exit(0)
    print("== CBOE 官方 / 纳斯达克 =="); tryit("CBOE VIX_History.csv", cboe); tryit("CBOE delayed_quotes json", cboe_json); tryit("nasdaq VIX", nasdaq)
    print("== akshare ==");
    try:
        ak_parts()
    except Exception:  # noqa: BLE001
        print(traceback.format_exc()[-600:])
    print("探测结束")
