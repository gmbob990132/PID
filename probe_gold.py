#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄金相关指标数据源探测（只读，不写库、不提交）。每个来源独立 try。用法：python3 probe_gold.py"""
import io, csv, json, re, sys, traceback
from datetime import date, datetime
from urllib.request import Request, urlopen
from urllib.parse import quote

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}


def get(url, timeout=45, headers=None):
    return urlopen(Request(url, headers={**UA, **(headers or {})}), timeout=timeout).read().decode("utf-8", "replace")


def num(x):
    try:
        v = float(str(x).replace(",", "").strip())
        return None if v != v else v
    except (TypeError, ValueError):
        return None


def report(name, rows):
    rows = sorted((str(d)[:10], v) for d, v in rows if v is not None)
    if not rows:
        print(f"  ✗ {name}: 0 行"); return
    print(f"  ✓ {name}: {len(rows)} 行  {rows[0][0]} ~ {rows[-1][0]}  最新 {rows[-1][1]}  前两条 {rows[-3:-1]}")


def tryit(name, fn):
    try:
        report(name, fn())
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {name}: {type(e).__name__} {str(e)[:200]}")


def sec(t):
    print("\n" + "=" * 72 + f"\n{t}\n" + "=" * 72, flush=True)


def col(df, *keys):
    return next(c for c in df.columns if any(k in str(c) for k in keys))


# ---------- A. 美国10年期实际利率（TIPS）----------
def treasury_real(year):
    url = (f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all"
           f"?type=daily_treasury_real_yield_curve&field_tdr_date_value={year}&page&_format=csv")
    r = list(csv.reader(io.StringIO(get(url, 60))))
    h = r[0]
    i = next(k for k, c in enumerate(h) if c.strip().upper().startswith("10 YR"))
    return [(datetime.strptime(a[0], "%m/%d/%Y").date().isoformat(), num(a[i])) for a in r[1:] if len(a) > i]


def treasury_nominal(year, tenor="10 YR"):
    url = (f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all"
           f"?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv")
    r = list(csv.reader(io.StringIO(get(url, 60))))
    i = next(k for k, c in enumerate(r[0]) if c.strip().upper() == tenor)
    return [(datetime.strptime(a[0], "%m/%d/%Y").date().isoformat(), num(a[i])) for a in r[1:] if len(a) > i]


def part_a():
    sec("A. 美国10年期实际利率（美国财政部 Daily Treasury Real Yield Curve，2003 起）")
    tryit("treasury 实际利率 10Y 2026", lambda: treasury_real(2026))
    tryit("treasury 实际利率 10Y 2010", lambda: treasury_real(2010))
    tryit("treasury 名义 2Y 2026", lambda: treasury_nominal(2026, "2 YR"))
    tryit("treasury 名义 3M 2026", lambda: treasury_nominal(2026, "3 MO"))


# ---------- B. 美元指数 ----------
def part_b():
    sec("B. 美元指数 DXY")
    import akshare as ak
    print("akshare", ak.__version__)
    try:
        sp = ak.index_global_spot_em()
        hit = sp[sp.astype(str).apply(lambda r: "美元" in " ".join(r), axis=1)]
        print("  东财全球指数中含『美元』：", hit.iloc[:, :3].to_string()[:400] if len(hit) else "无")
    except Exception as e:  # noqa: BLE001
        print("  index_global_spot_em 失败", type(e).__name__, str(e)[:120])
    for nm in ("美元指数",):
        tryit(f"index_global_hist_em({nm})", lambda nm=nm: (lambda df: list(zip(df[col(df, "日期")], map(num, df[col(df, "最新", "收盘")]))))(ak.index_global_hist_em(symbol=nm)))
    fn = getattr(ak, "futures_foreign_hist_sina", None) or getattr(ak, "futures_foreign_hist")
    for s in ("DINIW", "DX", "USDX"):
        tryit(f"sina 外盘 {s}", lambda s=s: (lambda df: list(zip(df["date"], map(num, df["close"]))))(fn(symbol=s)))
    tryit("stooq dx.f", lambda: [(a[0], num(a[4])) for a in list(csv.reader(io.StringIO(get("https://stooq.com/q/d/l/?s=dx.f&i=d"))))[1:] if len(a) > 4])


# ---------- C. 美联储政策利率与市场预期 ----------
def part_c():
    sec("C. 美联储：有效联邦基金利率（纽约联储）+ 2年美债（akshare）")
    def effr():
        j = json.loads(get("https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json?startDate=2000-01-01&endDate=" + date.today().isoformat(), 60))
        return [(r["effectiveDate"], num(r["percentRate"])) for r in j.get("refRates", [])]
    tryit("NY Fed EFFR", effr)
    import akshare as ak
    def us2y():
        df = (getattr(ak, "bond_zh_us_yield_em", None) or ak.bond_zh_us_rate)(start_date="19900101")
        print("    列：", list(df.columns))
        return list(zip(df[col(df, "日期")], map(num, df[col(df, "美国国债收益率2年")])))
    tryit("akshare 美债2年", us2y)


# ---------- D. 美国 CPI / 非农 ----------
def em_indicator(ind_id):
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_ECONOMICVALUE_USA&columns=ALL"
           f"&filter=(INDICATOR_ID=%22{ind_id}%22)&pageNumber=1&pageSize=2000&sortColumns=REPORT_DATE&sortTypes=-1&source=WEB&client=WEB")
    j = json.loads(get(url, 40))
    rows = ((j.get("result") or {}).get("data")) or []
    return [(r["REPORT_DATE"], num(r["VALUE"])) for r in rows]


def part_d():
    sec("D. 美国 CPI 同比 / 非农就业")
    import akshare as ak
    tryit("akshare macro_usa_cpi_yoy", lambda: (lambda df: list(zip(df[col(df, "时间")], map(num, df[col(df, "现值")]))))(ak.macro_usa_cpi_yoy()))
    for iid, nm in (("EMG00000733", "非农(东财猜测ID1)"), ("EMG00000736", "失业率(东财猜测ID)")):
        tryit(f"东财 {nm} {iid}", lambda iid=iid: em_indicator(iid))
    def bls(series):
        body = json.dumps({"seriesid": [series], "startyear": str(date.today().year - 9), "endyear": str(date.today().year)}).encode()
        req = Request("https://api.bls.gov/publicAPI/v1/timeseries/data/", data=body, headers={**UA, "Content-Type": "application/json"})
        j = json.loads(urlopen(req, timeout=60).read())
        if j.get("status") != "REQUEST_SUCCEEDED":
            print("    BLS:", j.get("message"))
        d = j["Results"]["series"][0]["data"]
        return [(f"{r['year']}-{r['period'][1:]}-01", num(r["value"])) for r in d if r["period"].startswith("M") and r["period"] != "M13"]
    tryit("BLS 非农就业人数 CES0000000001", lambda: bls("CES0000000001"))
    tryit("BLS CPI-U 指数 CUUR0000SA0", lambda: bls("CUUR0000SA0"))


# ---------- E. 央行黄金储备 ----------
def part_e():
    sec("E. 中国央行黄金储备（月度）")
    import akshare as ak
    for fn in ("macro_china_fx_gold_em", "macro_china_fx_gold_sina", "macro_china_fx_gold"):
        if hasattr(ak, fn):
            def f(fn=fn):
                df = getattr(ak, fn)()
                print(f"    {fn} 列：", list(df.columns)[:10])
                return list(zip(df[col(df, "月份", "统计时间", "日期")], map(num, df[col(df, "黄金储备")])))
            tryit(fn, f)
    print("  全球央行购金（世界黄金协会）：无公开接口，见说明")
    # IMF IFS：各国央行黄金储备（盎司）
    tryit("IMF IFS 中国黄金储备(盎司)", lambda: [(o["@TIME_PERIOD"], num(o["@OBS_VALUE"])) for o in json.loads(get(
        "http://dataservices.imf.org/REST/SDMX_JSON.svc/CompactData/IFS/M.CN.RAFAGOLDV_OZT?startPeriod=2015", 60))["CompactData"]["DataSet"]["Series"]["Obs"]])


# ---------- F. 黄金 ETF 持仓 ----------
def part_f():
    sec("F. 黄金 ETF 持仓（SPDR GLD）")
    import akshare as ak
    tryit("akshare fund_gold_etf_holding_js（金十）", lambda: (lambda df: list(zip(df["日期"], map(num, df["总库存"]))))(ak.fund_gold_etf_holding_js()))
    def spdr():
        txt = get("https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv", 60)
        r = list(csv.reader(io.StringIO(txt)))
        hi = next(i for i, a in enumerate(r) if a and a[0].strip().lower() == "date")
        h = [c.strip() for c in r[hi]]
        k = next(i for i, c in enumerate(h) if "Tonnes" in c)
        out = []
        for a in r[hi + 1:]:
            try:
                out.append((datetime.strptime(a[0].strip(), "%d-%b-%Y").date().isoformat(), num(a[k])))
            except (ValueError, IndexError):
                pass
        return out
    tryit("SPDR 官网 GLD 历史 CSV", spdr)


# ---------- G. CFTC 持仓 ----------
def part_g():
    sec("G. CFTC COMEX 黄金持仓（每周）")
    def cftc():
        url = ("https://publicreporting.cftc.gov/resource/6dca-aqww.json?cftc_contract_market_code=088691"
               "&$select=report_date_as_yyyy_mm_dd,noncomm_positions_long_all,noncomm_positions_short_all,open_interest_all"
               "&$order=report_date_as_yyyy_mm_dd&$limit=5000")
        rows = json.loads(get(url, 60))
        if rows:
            print("    最新一行：", rows[-1])
        return [(r["report_date_as_yyyy_mm_dd"], num(r["noncomm_positions_long_all"]) - num(r["noncomm_positions_short_all"])) for r in rows]
    tryit("CFTC 官方 Socrata（非商业净多头）", cftc)
    import akshare as ak
    def jin10():
        df = ak.futures_cftc_commodity_noncommercial_holding_js()
        gc = [c for c in df.columns if "黄金" in str(c)]
        print("    金十列（含黄金）：", gc)
        c = next(c for c in gc if "净" in str(c)) if any("净" in str(c) for c in gc) else gc[0]
        return list(zip(df["日期"], map(num, df[c])))
    tryit("akshare 金十 CFTC 非商业", jin10)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        assert num("1,2") == 12 and num("x") is None
        print("selftest OK"); sys.exit(0)
    for p in (part_a, part_b, part_c, part_d, part_e, part_f, part_g):
        try:
            p()
        except Exception:  # noqa: BLE001
            print("本节异常：", traceback.format_exc()[-700:])
    print("\n（VIX 已在美股模块用 CBOE 官网取到，这里不再测）\n探测结束")
