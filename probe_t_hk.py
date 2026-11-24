#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""做T 第一步补充探测：港股 30/60 分钟线 + Tushare A 股分钟线权限。只读。"""
import json
import os
import time
from urllib.request import Request, urlopen

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def get(url, referer=None, timeout=20):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        h["Referer"] = referer
    with urlopen(Request(url, headers=h), timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def show(name, url, referer=None):
    t0 = time.time()
    try:
        st, txt = get(url, referer)
        print(f"  [{name}] HTTP {st}  {len(txt)} 字节  {time.time()-t0:.1f}s")
        print("     开头：", txt[:300].replace("\n", " "))
        return txt
    except Exception as e:  # noqa: BLE001
        print(f"  [{name}] ✗ {type(e).__name__}: {str(e)[:150]}")
        return None


def count_qq(txt, sym, key):
    try:
        js = json.loads(txt)
        node = (js.get("data") or {}).get(sym) or {}
        print(f"     data[{sym}] 的键：{list(node.keys())[:10]}")
        arr = node.get(key) or []
        if arr:
            print(f"     → {key}: {len(arr)} 根  {arr[0][0]} → {arr[-1][0]}  最后一根 {arr[-1][:6]}")
    except Exception as e:  # noqa: BLE001
        print("     解析失败：", e)


def main():
    for num in ["00700", "00981"]:
        sym = "hk" + num
        print(f"\n== {num}.HK ==")
        for p in (30, 60):
            for host in ("ifzq.gtimg.cn", "web.ifzq.gtimg.cn"):
                t = show(f"腾讯 {host} m{p}",
                         f"https://{host}/appstock/app/kline/mkline?param={sym},m{p},,320",
                         "https://gu.qq.com/")
                if t:
                    count_qq(t, sym, f"m{p}")
        # 腾讯港股当日分时（只有当天，用来判断能否"每天自己累积"）
        show("腾讯 当日分时", f"https://web.ifzq.gtimg.cn/appstock/app/hkMinute/query?code={sym}",
             "https://gu.qq.com/")
        # Yahoo：60m 最多约 730 天，30m 最多约 60 天
        ysym = num.lstrip("0").zfill(4) + ".HK"
        for itv, rng in (("60m", "200d"), ("30m", "60d")):
            t = show(f"Yahoo {itv}",
                     f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?interval={itv}&range={rng}")
            if t:
                try:
                    r = json.loads(t)["chart"]["result"][0]
                    print(f"     → {len(r.get('timestamp') or [])} 根")
                except Exception as e:  # noqa: BLE001
                    print("     解析失败：", e)
        # 东财港股（上次被断开，再试一次，换 host）
        for host in ("push2his.eastmoney.com", "33.push2his.eastmoney.com"):
            show(f"东财 {host} 60",
                 f"https://{host}/api/qt/stock/kline/get?secid=116.{num}&fields1=f1&fields2=f51,f53"
                 f"&klt=60&fqt=1&end=20500101&lmt=300", "https://quote.eastmoney.com/")
        time.sleep(1)

    print("\n== Tushare A 股分钟线权限（600900.SH 60min）==")
    tok = os.environ.get("TUSHARE_TOKEN")
    if not tok:
        print("  没有 TUSHARE_TOKEN")
        return
    try:
        import tushare as ts
        ts.set_token(tok)
        df = ts.pro_bar(ts_code="600900.SH", freq="60min", adj="qfq",
                        start_date="2026-10-01 09:00:00", end_date="2026-11-30 16:00:00")
        print("  返回：", None if df is None else f"{len(df)} 行", "" if df is None else df.head(3).to_string())
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {type(e).__name__}: {str(e)[:200]}")


if __name__ == "__main__":
    main()
