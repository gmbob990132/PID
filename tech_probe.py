#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技术指标分析模块 · 数据源探路。在 GitHub Actions 上跑一次（workflow_dispatch → probe-tech），
验证 GitHub 境外 runner 能否：
  1) 连上 Tushare 并用 pro_bar 取 A 股前复权日线（含你账号的权限/限速表现）
  2) stock_basic 补全股票名称
  3) 访问腾讯财经港股 K 线与行情接口
最后一行汇总打印 PROBE RESULT，方便一眼看结论。Token 不会被打印。
"""
import os
import time
import json
from urllib.request import Request, urlopen

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
res = {}


def step(name, fn):
    t0 = time.time()
    try:
        info = fn()
        res[name] = "OK"
        print(f"[OK ] {name} ({time.time() - t0:.1f}s) {info or ''}")
    except Exception as e:  # noqa: BLE001
        res[name] = "FAIL"
        print(f"[FAIL] {name} ({time.time() - t0:.1f}s) {repr(e)[:300]}")


def main():
    token = os.environ.get("TUSHARE_TOKEN", "")
    print("TUSHARE_TOKEN 已设置:", bool(token), "长度:", len(token))

    if token:
        import tushare as ts
        ts.set_token(token)

        def pro_bar():
            df = ts.pro_bar(ts_code="600031.SH", adj="qfq", start_date="20260901", end_date="20261009", asset="E", freq="D")
            assert df is not None and not df.empty, "返回为空"
            return f"{len(df)} 行, 列={list(df.columns)}, 最新={df['trade_date'].max()}, close={df.sort_values('trade_date')['close'].iloc[-1]}"
        step("tushare_pro_bar_qfq", pro_bar)

        def burst():
            ok = 0
            for _ in range(6):
                df = ts.pro_bar(ts_code="000063.SZ", adj="qfq", start_date="20261001", end_date="20261009", asset="E", freq="D")
                ok += int(df is not None and not df.empty)
            return f"连续 6 次均成功: {ok}/6"
        step("tushare_burst_6_calls", burst)

        def basic():
            pro = ts.pro_api()
            df = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name")
            assert df is not None and len(df) > 1000, f"仅 {0 if df is None else len(df)} 行"
            return f"{len(df)} 只 A 股名称"
        step("tushare_stock_basic", basic)
    else:
        res["tushare_pro_bar_qfq"] = res["tushare_burst_6_calls"] = res["tushare_stock_basic"] = "SKIP(no token)"

    def hk_kline():
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=hk00700,day,2026-09-01,2026-10-09,800,qfq"
        with urlopen(Request(url, headers={"User-Agent": UA}), timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        inner = (data.get("data") or {}).get("hk00700") or {}
        k = inner.get("qfqday") or inner.get("day") or []
        assert k, "无 K 线"
        return f"{len(k)} 根, 末根={k[-1][:3]}"
    step("tencent_hk_kline", hk_kline)

    def hk_name():
        with urlopen(Request("https://qt.gtimg.cn/q=hk00700", headers={"User-Agent": UA}), timeout=10) as r:
            txt = r.read().decode("gbk", errors="replace")
        return "名称=" + txt.split('"')[1].split("~")[1]
    step("tencent_hk_name", hk_name)

    print("\nPROBE RESULT:", json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
