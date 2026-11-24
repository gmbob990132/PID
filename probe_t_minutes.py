#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
做T 模块 · 第一步探测：30 分钟 / 60 分钟 K 线能从哪里拿到（在 GitHub Actions 上跑）

要回答的问题：
  1) 每个来源能不能连上（东方财富 / 新浪 / 腾讯 / Tushare）
  2) 能拿到多少根 K 线、最早到哪天 —— MA233 至少要 233 根
  3) 是否前复权（不复权的话，窗口里有除权会让均线失真）
  4) 各来源最新一根收盘价是否一致

只读、不写库。用法：python3 probe_t_minutes.py
"""
import json
import os
import re
import time
import traceback
from urllib.request import Request, urlopen

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SAMPLES = ["600900.SH", "300750.SZ", "688041.SH", "00700.HK", "00981.HK"]
PERIODS = [30, 60]


def get(url, referer=None, timeout=20):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        h["Referer"] = referer
    with urlopen(Request(url, headers=h), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def split(code):
    num, mkt = code.split(".")
    return num, mkt


# ---------------- 各来源：返回 [(时间str, 收盘float)]、复权说明 ----------------
def src_em(code, p):
    num, mkt = split(code)
    secid = {"SH": "1", "SZ": "0", "BJ": "0", "HK": "116"}[mkt] + "." + num
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57"
           f"&klt={p}&fqt=1&end=20500101&lmt=1500")
    js = json.loads(get(url, "https://quote.eastmoney.com/"))
    kl = ((js.get("data") or {}).get("klines")) or []
    rows = []
    for s in kl:
        f = s.split(",")
        rows.append((f[0], float(f[2])))
    return rows, "前复权(fqt=1)"


def src_sina(code, p):
    num, mkt = split(code)
    if mkt == "HK":
        raise RuntimeError("新浪分钟线接口不支持港股，跳过")
    sym = ("sh" if mkt == "SH" else ("bj" if mkt == "BJ" else "sz")) + num
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sym}&scale={p}&ma=no&datalen=1023")
    txt = get(url, "https://finance.sina.com.cn/")
    arr = json.loads(txt) if txt.strip().startswith("[") else []
    return [(x["day"], float(x["close"])) for x in arr], "不复权"


def src_qq(code, p):
    num, mkt = split(code)
    sym = ("hk" + num) if mkt == "HK" else (mkt.lower() + num)
    url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sym},m{p},,1500"
    js = json.loads(get(url, "https://gu.qq.com/"))
    node = ((js.get("data") or {}).get(sym)) or {}
    arr = node.get(f"m{p}") or []
    rows = []
    for x in arr:                       # [YYYYMMDDHHMM, open, close, high, low, vol, ...]
        rows.append((str(x[0]), float(x[2])))
    return rows, "不复权"


def src_tushare(code, p):
    tok = os.environ.get("TUSHARE_TOKEN")
    if not tok:
        raise RuntimeError("没有 TUSHARE_TOKEN")
    if code.endswith(".HK"):
        raise RuntimeError("Tushare 港股分钟线需另外权限，跳过")
    import tushare as ts
    ts.set_token(tok)
    from datetime import date, timedelta
    end = date.today()
    start = end - timedelta(days=120)
    df = ts.pro_bar(ts_code=code, freq=f"{p}min", adj="qfq",
                    start_date=start.strftime("%Y-%m-%d 09:00:00"),
                    end_date=end.strftime("%Y-%m-%d 16:00:00"))
    if df is None or len(df) == 0:
        raise RuntimeError("返回空（多半是没有分钟线权限）")
    df = df.sort_values("trade_time")
    return list(zip(df["trade_time"].astype(str), df["close"].astype(float))), "前复权(adj=qfq)"


SOURCES = [("东方财富", src_em), ("新浪", src_sina), ("腾讯", src_qq), ("Tushare", src_tushare)]


def main():
    print("做T 分钟线探测  样本：", ", ".join(SAMPLES), "\n")
    summary = {}
    for code in SAMPLES:
        for p in PERIODS:
            print(f"== {code}  {p} 分钟 ==")
            lasts = {}
            for name, fn in SOURCES:
                t0 = time.time()
                try:
                    rows, adj = fn(code, p)
                    dt = time.time() - t0
                    if not rows:
                        raise RuntimeError("0 根")
                    n = len(rows)
                    ok233 = "够" if n >= 233 else "不够"
                    print(f"  ✓ {name:<8} {n:>5} 根  {rows[0][0]} → {rows[-1][0]}  "
                          f"最新收盘 {rows[-1][1]:<9} {adj}  MA233:{ok233}  {dt:.1f}s")
                    lasts[name] = rows[-1][1]
                    summary.setdefault((name, p), []).append(n)
                except Exception as e:  # noqa: BLE001
                    dt = time.time() - t0
                    msg = f"{type(e).__name__}: {str(e)[:120]}"
                    print(f"  ✗ {name:<8} {msg}  {dt:.1f}s")
                    summary.setdefault((name, p), []).append(0)
                time.sleep(0.5)
            if len(set(round(v, 3) for v in lasts.values())) > 1:
                print(f"  ⚠ 各来源最新收盘不一致：{lasts}（复权口径不同时属正常，看差距大小）")
            print()

    print("==== 汇总：每个来源 × 周期，在各样本上拿到的根数 ====")
    for (name, p), ns in sorted(summary.items(), key=lambda x: (x[0][1], x[0][0])):
        ok = sum(1 for n in ns if n >= 233)
        print(f"  {p:>2} 分钟  {name:<8} 根数 {ns}   满足 MA233 的样本 {ok}/{len(ns)}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
