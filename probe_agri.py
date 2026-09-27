#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""农产品期货探路：验证 11 个品种主力连续能否取到、有多长历史。GitHub 上 pip install akshare 后跑。"""
import akshare as ak
from datetime import date, timedelta

# 品种名: 主力连续代码（郑商所/大商所）
SYMBOLS = [
    ("白糖", "SR0"), ("棉花", "CF0"), ("苹果", "AP0"), ("红枣", "CJ0"),
    ("花生", "PK0"), ("棕榈油", "P0"), ("玉米", "C0"), ("玉米淀粉", "CS0"),
    ("豆粕", "M0"), ("鸡蛋", "JD0"), ("生猪", "LH0"),
]
start = "20150101"   # 试着从2015取，看各品种实际最早到哪天


def main():
    for name, sym in SYMBOLS:
        try:
            df = ak.futures_main_sina(symbol=sym, start_date=start, end_date="22220101")
            col = "收盘价" if "收盘价" in df.columns else next((c for c in df.columns if "close" in str(c).lower()), "?")
            dcol = "日期" if "日期" in df.columns else "?"
            first = str(df[dcol].iloc[0])[:10] if dcol in df.columns and len(df) else "?"
            last = str(df[dcol].iloc[-1])[:10] if dcol in df.columns and len(df) else "?"
            close = df[col].iloc[-1] if col in df.columns and len(df) else "?"
            print(f"✓ {name:<5} {sym:<5} {len(df):>5}天  {first}~{last}  最新收盘 {close}")
        except Exception as e:
            print(f"!! {name:<5} {sym:<5} 失败: {repr(e)[:120]}")


if __name__ == "__main__":
    main()
