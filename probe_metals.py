#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
金属模块 · 数据源探路。在 GitHub 上跑一次（pip install akshare），
验证金/铜/锂的国内外价格能否取到、symbol 对不对，并打印最新值。
"""
import akshare as ak
from datetime import date, timedelta

start = (date.today() - timedelta(days=15)).strftime("%Y%m%d")


def show(title, fn):
    print(f"\n=== {title} ===")
    try:
        df = fn()
        print("  列：", list(df.columns))
        print(df.tail(3).to_string(index=False))
    except Exception as e:
        print("  !! 失败：", repr(e)[:180])


def main():
    # ---------- 国内期货主力（沪金/沪铜/碳酸锂）——应该都稳 ----------
    for name, sym in [("沪金 AU0", "AU0"), ("沪铜 CU0", "CU0"), ("碳酸锂 LC0", "LC0")]:
        show(f"国内期货 {name}", lambda s=sym: ak.futures_main_sina(symbol=s, start_date=start, end_date="22220101"))

    # ---------- 上海金交所现货（上海金 Au99.99）----------
    show("上海金 Au99.99", lambda: ak.spot_hist_sge(symbol="Au99.99"))

    # ---------- 外盘：先列出所有可用 symbol，再试伦敦金/COMEX金/LME铜 ----------
    print("\n=== 外盘可用 symbol 列表（找金 Gold / 铜 Copper）===")
    try:
        syms = ak.futures_foreign_commodity_subscribe_exchange_symbol()
        print("  ", syms)
    except Exception as e:
        print("  !! 取 symbol 列表失败：", repr(e)[:150])

    # 常见猜测：伦敦金 GC(COMEX) / XAU；LME铜 CAD(3个月)。逐个试。
    for name, sym in [("COMEX金 GC", "GC"), ("伦敦金 XAU", "XAU"),
                      ("LME铜 CAD", "CAD"), ("LME铜 CA", "CA")]:
        show(f"外盘 {name}", lambda s=sym: ak.futures_foreign_hist(symbol=s))

    # ---------- LME 铜库存（复用电解铝那个宽表，金属=铜）----------
    show("LME 铜库存（宽表，找'铜'列）", lambda: ak.macro_euro_lme_stock())


if __name__ == "__main__":
    main()
