#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ETF 份额 + 融资融券 数据源探测：先看 Tushare 权限够不够，再顺带测 akshare(交易所官网) 作为备选。只读，不写任何文件。"""
import os, sys, json, time, traceback
from datetime import date, timedelta

R = {}          # 结果汇总（最后整体打印）
def step(name):
    def deco(fn):
        t0 = time.time()
        try:
            R[name] = {"ok": True, **(fn() or {}), "sec": round(time.time() - t0, 1)}
        except Exception as e:  # noqa: BLE001
            R[name] = {"ok": False, "error": (type(e).__name__ + ": " + str(e))[:300], "sec": round(time.time() - t0, 1)}
        print(f"[{name}]", json.dumps(R[name], ensure_ascii=False)[:600], flush=True)
        return fn
    return deco

def span(df, col):
    if df is None or len(df) == 0:
        return {"rows": 0}
    s = sorted(str(x) for x in df[col])
    return {"rows": len(df), "first": s[0], "last": s[-1]}

today = date.today()
d3y = (today - timedelta(days=365 * 3 + 5)).strftime("%Y%m%d")
dnow = today.strftime("%Y%m%d")
d30 = (today - timedelta(days=30)).strftime("%Y%m%d")

# ---------- A. Tushare ----------
pro = None
tok = os.environ.get("TUSHARE_TOKEN", "")
if tok:
    import tushare as ts
    ts.set_token(tok)
    pro = ts.pro_api()
else:
    R["tushare_token"] = {"ok": False, "error": "环境变量 TUSHARE_TOKEN 为空"}

if pro is not None:
    @step("ts_fund_share_510300_3y")           # ETF 总份额，近3年
    def _():
        return span(pro.fund_share(ts_code="510300.SH", start_date=d3y, end_date=dnow), "trade_date")

    @step("ts_fund_share_159919_3y")
    def _():
        return span(pro.fund_share(ts_code="159919.SZ", start_date=d3y, end_date=dnow), "trade_date")

    @step("ts_fund_share_fields")
    def _():
        df = pro.fund_share(ts_code="510300.SH", start_date=d30, end_date=dnow)
        return {"columns": list(df.columns), "sample": df.head(2).to_dict("records")}

    @step("ts_fund_basic_etf")                 # ETF 清单（用来挑中证500头部6只）
    def _():
        df = pro.fund_basic(market="E", status="L")
        m = df[df["benchmark"].astype(str).str.contains("中证500", na=False)]
        return {"etf_total": len(df), "csi500_like": len(m),
                "csi500_codes": m[["ts_code", "name"]].head(40).values.tolist()}

    @step("ts_fund_daily_510500")              # 用于 规模 = 份额×价格 排序
    def _():
        df = pro.fund_daily(ts_code="510500.SH", start_date=d30, end_date=dnow)
        return {**span(df, "trade_date"), "columns": list(df.columns)[:12]}

    @step("ts_margin_3y")                      # 融资融券交易所汇总
    def _():
        df = pro.margin(start_date=d3y, end_date=dnow)
        return {**span(df, "trade_date"), "columns": list(df.columns)}

    @step("ts_margin_recent")
    def _():
        df = pro.margin(start_date=d30, end_date=dnow)
        return {**span(df, "trade_date"), "sample": df.head(2).to_dict("records")}

# ---------- B. akshare（交易所官网）备选 ----------
try:
    import akshare as ak
except Exception as e:  # noqa: BLE001
    ak = None
    R["akshare_import"] = {"ok": False, "error": str(e)[:200]}

if ak is not None:
    def last_weekday():
        d = today - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d

    @step("ak_etf_scale_sse_recent")           # 上交所 ETF 份额，某一天
    def _():
        d = last_weekday().strftime("%Y%m%d")
        df = ak.fund_etf_scale_sse(date=d)
        row = df[df.astype(str).apply(lambda r: r.str.contains("510300").any(), axis=1)]
        return {"date": d, "rows": len(df), "columns": list(df.columns), "510300": row.head(1).to_dict("records")}

    @step("ak_etf_scale_sse_old")              # 3 年前能不能取到
    def _():
        d = date(today.year - 3, today.month, min(today.day, 28))
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        df = ak.fund_etf_scale_sse(date=d.strftime("%Y%m%d"))
        return {"date": d.isoformat(), "rows": len(df)}

    @step("ak_etf_scale_szse")                 # 深交所 ETF 份额
    def _():
        df = ak.fund_etf_scale_szse()
        row = df[df.astype(str).apply(lambda r: r.str.contains("159919").any(), axis=1)]
        return {"rows": len(df), "columns": list(df.columns), "159919": row.head(1).to_dict("records")}

    @step("ak_margin_sse")
    def _():
        df = ak.stock_margin_sse(start_date=d3y, end_date=dnow)
        return {"rows": len(df), "columns": list(df.columns), "first": str(df.iloc[0, 0]), "last": str(df.iloc[-1, 0])}

    @step("ak_margin_szse")
    def _():
        d = last_weekday().strftime("%Y%m%d")
        df = ak.stock_margin_szse(date=d)
        return {"date": d, "rows": len(df), "columns": list(df.columns), "data": df.head(2).to_dict("records")}

print("\nPROBE RESULT:", json.dumps({k: v.get("ok") for k, v in R.items()}, ensure_ascii=False))
