#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中芯国际 00981.HK、华虹半导体 01347.HK 日线前复权收盘价（2023-11-01 至今）→ hk_qfq/

来源按顺序尝试，每只股票用第一个成功的：
  1. Tushare hk_daily_adj（港股复权行情：自带 adj_factor 时按 前复权 = 收盘 × 当日因子 ÷ 最新因子 计算）
  2. Tushare pro_bar(asset="HK", adj="qfq")
  3. akshare 东财 stock_hk_hist_em(adjust="qfq")（备用）
输出：
  hk_qfq/hk_qfq_close.csv    宽表：日期, 中芯国际, 华虹半导体（UTF-8 BOM，Excel 直接打开不乱码）
  hk_qfq/hk_qfq_close.xlsx   同内容 + 每只股票一个明细 sheet（含不复权收盘、复权因子、来源）
用法：TUSHARE_TOKEN=xxx python3 hk_qfq_export.py
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


def ts_hk_daily_adj(pro, code, _):
    df = pro.hk_daily_adj(ts_code=code, start_date=START, end_date=END)
    if df is None or df.empty:
        raise RuntimeError("hk_daily_adj 无数据（可能没有该接口权限）")
    print(f"    hk_daily_adj 列：{list(df.columns)}")
    df = df.sort_values("trade_date")
    if "adj_factor" in df.columns and df["adj_factor"].notna().any():
        # 前复权以"区间最后一天"为基准；用全历史最新因子更严谨，区间截到今天即等价
        f = pd.to_numeric(df["adj_factor"], errors="coerce")
        df["qfq"] = pd.to_numeric(df["close"], errors="coerce") * f / f.iloc[-1]
        out = norm(df, "trade_date", "qfq")
        raw = df[["trade_date", "close", "adj_factor"]].copy()
        return out, raw
    for c in ("close_qfq", "qfq_close"):
        if c in df.columns:
            return norm(df, "trade_date", c), df
    raise RuntimeError("hk_daily_adj 返回里没有复权因子或前复权价")


def ts_pro_bar(pro, code, _):
    import tushare as ts
    df = ts.pro_bar(ts_code=code, asset="HK", adj="qfq", start_date=START, end_date=END, api=pro)
    if df is None or df.empty:
        raise RuntimeError("pro_bar 无数据")
    return norm(df, "trade_date", "close"), df


def ak_em(_, __, sym):
    import akshare as ak
    fn = getattr(ak, "stock_hk_hist_em", None) or getattr(ak, "stock_hk_hist")
    df = fn(symbol=sym, period="daily", start_date=START, end_date=END, adjust="qfq")
    if df is None or df.empty:
        raise RuntimeError("东财无数据")
    return norm(df, "日期", "收盘"), df


def main():
    tok = os.environ.get("TUSHARE_TOKEN")
    pro = None
    if tok:
        import tushare as ts
        pro = ts.pro_api(tok)
    else:
        print("未设置 TUSHARE_TOKEN，只用 akshare 备用来源")
    os.makedirs(OUT, exist_ok=True)
    wide, details = None, {}
    for code, sym, name in STOCKS:
        got = None
        for label, fn in (("tushare hk_daily_adj", ts_hk_daily_adj), ("tushare pro_bar qfq", ts_pro_bar), ("akshare 东财 qfq", ak_em)):
            if label.startswith("tushare") and pro is None:
                continue
            try:
                s, raw = fn(pro, code, sym)
                if len(s) < 20:
                    raise RuntimeError(f"只有 {len(s)} 行")
                got = (label, s, raw)
                print(f"✓ {name} {code}：{label}，{len(s)} 行，{s['date'].iloc[0]} ~ {s['date'].iloc[-1]}，最新前复权收盘 {s['close_qfq'].iloc[-1]:.3f}")
                break
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ {name} {label}：{type(e).__name__} {str(e)[:160]}")
        if not got:
            print(f"!! {name} 所有来源都失败"); continue
        label, s, raw = got
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
