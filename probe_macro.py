#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宏观指标探路：每个数据源打印 形状/列名/首尾各2行；国家统计局指标树里搜 房地产/利润/70城。只读。"""
import json, re, sys, os, traceback
from urllib.request import Request, urlopen
from urllib.parse import urlencode
import pandas as pd
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def show(tag, df):
    if df is None or not len(df):
        print(f"!! {tag}: 空"); return
    print(f"✓ {tag}: {df.shape}  列={list(df.columns)}")
    print(df.head(2).to_string()[:500]); print(df.tail(2).to_string()[:500])


def ak_try(tag, fn, **kw):
    import akshare as ak
    try:
        show(tag, getattr(ak, fn)(**kw))
    except Exception as e:  # noqa: BLE001
        print(f"!! {tag} ({fn}): {repr(e)[:160]}")


def probe_ak():
    print("\n===== akshare =====")
    for tag, fn, kw in [
        ("CPI同比(金十,1986起)", "macro_china_cpi_yearly", {}),
        ("CPI月率(金十)", "macro_china_cpi_monthly", {}),
        ("PPI同比(金十,1995起)", "macro_china_ppi_yearly", {}),
        ("CPI(东财)", "macro_china_cpi", {}),
        ("PPI(东财)", "macro_china_ppi", {}),
        ("PMI(金十,2005起)", "macro_china_pmi_yearly", {}),
        ("PMI(东财)", "macro_china_pmi", {}),
        ("M2同比(金十,1998起)", "macro_china_m2_yearly", {}),
        ("货币供应(东财 M0/M1/M2)", "macro_china_money_supply", {}),
        ("社融增量(商务部)", "macro_china_shrzgm", {}),
        ("新增信贷", "macro_china_new_financial_credit", {}),
        ("国房景气指数", "macro_china_real_estate", {}),
        ("新房价(北京,上海)", "macro_china_new_house_price", {"city_first": "北京", "city_second": "上海"}),
        ("固定资产投资(东财)", "macro_china_gdzctz", {}),
        ("中美国债收益率", "bond_zh_us_rate", {"start_date": "20000101"}),
        ("人民币中间价(2017起)", "macro_china_rmb", {}),
        ("USDCNH日线", "forex_hist_em", {"symbol": "USDCNH"}),
        ("LPR", "macro_china_lpr", {}),
    ]:
        ak_try(tag, fn, **kw)


def probe_tushare():
    print("\n===== tushare =====")
    tok = os.environ.get("TUSHARE_TOKEN")
    if not tok:
        print("无 TUSHARE_TOKEN，跳过"); return
    import tushare as ts
    pro = ts.pro_api(tok)
    for tag, fn, kw in [("cn_cpi", "cn_cpi", {"start_m": "202401", "end_m": "202610"}),
                        ("cn_ppi", "cn_ppi", {"start_m": "202401", "end_m": "202610"}),
                        ("cn_pmi", "cn_pmi", {"start_m": "202401", "end_m": "202610"}),
                        ("cn_m", "cn_m", {"start_m": "202401", "end_m": "202610"}),
                        ("sf_month(社融)", "sf_month", {"start_m": "202401", "end_m": "202610"})]:
        try:
            show(tag, getattr(pro, fn)(**kw))
        except Exception as e:  # noqa: BLE001
            print(f"!! {tag}: {repr(e)[:160]}")


def nbs_get(params):
    url = "https://data.stats.gov.cn/easyquery.htm?" + urlencode(params)
    return json.loads(urlopen(Request(url, headers=UA), timeout=30).read().decode("utf-8", "ignore"))


def probe_nbs():
    print("\n===== 国家统计局 data.stats.gov.cn 月度指标树 =====")
    pat = re.compile(r"房地产|商品房|住宅|工业企业|利润|社会融资|70")
    seen = 0

    def walk(pid, depth, path):
        nonlocal seen
        if depth > 3 or seen > 80:
            return
        try:
            nodes = nbs_get({"id": pid, "dbcode": "hgyd", "wdcode": "zb", "m": "getTree"})
        except Exception as e:  # noqa: BLE001
            print("!! getTree", pid, repr(e)[:120]); return
        for n in nodes:
            nm = n.get("name", "")
            if pat.search(nm):
                seen += 1
                print("  ", "/".join(path + [nm]), "→", n.get("id"), "(父节点)" if n.get("isParent") else "(指标)")
            if n.get("isParent") and depth < 3:
                walk(n["id"], depth + 1, path + [nm])
    walk("zb", 0, [])
    print("\n--- 试取一个指标（若上面找到房地产开发投资，把 id 换进来再测）---")
    try:
        j = nbs_get({"m": "QueryData", "dbcode": "hgyd", "rowcode": "zb", "colcode": "sj",
                     "wds": "[]", "dfwds": json.dumps([{"wdcode": "zb", "valuecode": "A010101"}, {"wdcode": "sj", "valuecode": "LAST6"}])})
        print("QueryData 返回状态：", j.get("returncode"), "，数据点数：", len(j.get("returndata", {}).get("datanodes", [])))
    except Exception as e:  # noqa: BLE001
        print("!! QueryData", repr(e)[:160])


if __name__ == "__main__":
    for f in (probe_ak, probe_tushare, probe_nbs):
        try:
            f()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
