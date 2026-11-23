#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中芯国际 00981.HK、华虹半导体 01347.HK 日线前复权收盘价（2023-11-01 至今）→ hk_qfq/

来源（不用 Tushare）：
  1. 东方财富 前复权日线（akshare stock_hk_hist_em, adjust="qfq"；限流时等待后重试）
  2. 新浪财经 前复权日线（akshare stock_hk_hist_sina, adjust="qfq"）
两个都取到时以东方财富为准，并打印两者的最大差异，方便核对。
输出：
  hk_qfq/hk_qfq_close.csv    宽表：日期, 中芯国际, 华虹半导体（UTF-8 BOM，Excel 直接打开不乱码）
  hk_qfq/hk_qfq_close.xlsx   同内容 + 每只股票一个明细 sheet（含不复权收盘、复权因子、来源）
用法：python3 hk_qfq_export.py
"""
import os, sys
import pandas as pd

START, END = "20231101", pd.Timestamp.today().strftime("%Y%m%d")
STOCKS = [("00981.HK", "00981", "中芯国际"), ("01347.HK", "01347", "华虹半导体")]
OUT = "hk_qfq"


def norm(df, date_col, close_col):
    d = pd.DataFrame({"date": pd.to_datetime(df[date_col].astype(str)).dt.strftime("%Y-%m-%d"),
                      "close_qfq": pd.to_numeric(df[close_col], errors="coerce")})
    return d.dropna().sort_values("date").drop_duplicates("date")


def ak_em(sym):
    import time
    import akshare as ak
    fn = getattr(ak, "stock_hk_hist_em", None) or getattr(ak, "stock_hk_hist")
    for k in range(3):
        try:
            df = fn(symbol=sym, period="daily", start_date=START, end_date=END, adjust="qfq")
            if df is None or df.empty:
                raise RuntimeError("东财无数据")
            return norm(df, "日期", "收盘"), df
        except Exception as e:  # noqa: BLE001
            if k == 2:
                raise
            print(f"    东财第 {k + 1} 次失败（{type(e).__name__}），{60 * (k + 1)} 秒后重试")
            time.sleep(60 * (k + 1))


def ak_sina(sym):
    import akshare as ak
    df = ak.stock_hk_hist_sina(symbol=sym, period="daily", adjust="qfq")
    if df is None or df.empty:
        raise RuntimeError("新浪无数据")
    dc = next(c for c in df.columns if str(c).lower() in ("date", "日期"))
    cc = next(c for c in df.columns if str(c).lower() in ("close", "收盘"))
    s = norm(df, dc, cc)
    s = s[(s["date"] >= f"{START[:4]}-{START[4:6]}-{START[6:]}")]
    return s, df


def main():
    os.makedirs(OUT, exist_ok=True)
    wide, details = None, {}
    for code, sym, name in STOCKS:
        res = {}
        for label, fn in (("东方财富 前复权", ak_em), ("新浪 前复权", ak_sina)):
            try:
                s, raw = fn(sym)
                if len(s) < 20:
                    raise RuntimeError(f"只有 {len(s)} 行")
                res[label] = (s, raw)
                print(f"✓ {name} {code}：{label}，{len(s)} 行，{s['date'].iloc[0]} ~ {s['date'].iloc[-1]}，最新前复权收盘 {s['close_qfq'].iloc[-1]:.3f}")
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ {name} {label}：{type(e).__name__} {str(e)[:160]}")
        if not res:
            print(f"!! {name} 所有来源都失败"); continue
        if len(res) == 2:
            a_, b_ = [v[0].set_index("date")["close_qfq"] for v in res.values()]
            j = a_.to_frame("a").join(b_.to_frame("b"), how="inner")
            if len(j):
                rel = ((j["a"] - j["b"]).abs() / j["b"]).max()
                print(f"    两个来源共同 {len(j)} 天，最大相对差异 {rel:.4%}")
        label = next(iter(res))
        s, raw = res[label]
        s = s.rename(columns={"close_qfq": name})
        wide = s if wide is None else wide.merge(s, on="date", how="outer")
        raw = raw.copy(); raw["来源"] = label
        details[name] = raw
    if wide is None:
        sys.exit(1)
    wide = wide.sort_values("date").rename(columns={"date": "日期"})
    wide.to_csv(f"{OUT}/hk_qfq_close.csv", index=False, encoding="utf-8-sig", float_format="%.4f")
    with pd.ExcelWriter(f"{OUT}/hk_qfq_close.xlsx") as w:
        wide.to_excel(w, sheet_name="前复权收盘价", index=False)
        for nm, df in details.items():
            df.to_excel(w, sheet_name=nm[:28], index=False)
    print(f"已写出 {OUT}/hk_qfq_close.csv、.xlsx：{len(wide)} 个交易日")
    print(wide.tail(5).to_string(index=False))


if __name__ == "__main__":
    main()
