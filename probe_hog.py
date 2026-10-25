#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生猪出栏价探路：验证 akshare 里几个生猪现货/指数接口能否取到、历史多长、单位如何。
GitHub 上 pip install akshare 后跑；只读，不写库。"""
import traceback
import pandas as pd
import akshare as ak

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 20)


def show(tag, fn, date_col=None, val_cols=None):
    print(f"\n===== {tag} =====")
    try:
        df = fn()
        print(f"行数 {len(df)}  列 {list(df.columns)}")
        if date_col and date_col in df.columns and len(df):
            d = pd.to_datetime(df[date_col], errors="coerce").dropna()
            print(f"日期范围 {d.min().date()} ~ {d.max().date()}（共 {d.nunique()} 个不同日期）")
            gaps = d.sort_values().diff().dt.days.dropna()
            if len(gaps):
                print(f"相邻日期间隔：中位数 {gaps.median()} 天，最大 {gaps.max()} 天")
        print("最近 8 行：")
        print(df.tail(8).to_string(index=False))
        print("最早 3 行：")
        print(df.head(3).to_string(index=False))
        if val_cols:
            for c in val_cols:
                if c in df.columns:
                    s = pd.to_numeric(df[c], errors="coerce").dropna()
                    if len(s):
                        print(f"[{c}] 最小 {s.min()}  最大 {s.max()}  最新 {s.iloc[-1]}")
    except Exception as e:  # noqa: BLE001
        print(f"!! 失败：{repr(e)[:300]}")
        traceback.print_exc(limit=2)


def main():
    print("akshare 版本：", ak.__version__)
    # 1. 玄田数据：全国 外三元 / 内三元 / 土杂猪 出栏均价（日度，单位预计元/公斤）
    for sym in ("外三元", "内三元", "土杂猪"):
        show(f"futures_hog_core({sym}) 玄田数据 全国出栏均价", lambda s=sym: ak.futures_hog_core(symbol=s),
             date_col="date", val_cols=["value"])
    # 2. 搜猪网：今年以来全国出栏均价走势
    show("spot_hog_year_trend_soozhu 搜猪网 今年以来全国出栏均价", ak.spot_hog_year_trend_soozhu,
         date_col="日期", val_cols=["价格"])
    # 3. 搜猪网：各省均价（只看当前一天的截面）
    show("spot_hog_soozhu 搜猪网 各省均价实时", ak.spot_hog_soozhu, val_cols=["价格"])
    # 4. 行情宝：生猪价格指数（含成交均价、成交均重）
    show("index_hog_spot_price 行情宝 生猪价格指数", ak.index_hog_spot_price,
         date_col="日期", val_cols=["指数", "成交均价", "成交均重"])
    # 5. 玄田数据：猪肉批发价（供给侧参考）
    show("futures_hog_supply(猪肉批发价) 玄田数据", lambda: ak.futures_hog_supply(symbol="猪肉批发价"),
         date_col="date", val_cols=["value"])


if __name__ == "__main__":
    main()
