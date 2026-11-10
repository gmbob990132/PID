#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
宏观数据模块 · 采集落库
  指标登记：macro_config/indicators.json   落库：data/macro.db（macro_series 长表）
  每个指标独立取数，失败只影响自己（记日志，不中断）。
用法：
  python3 macro_ingest.py              # 取全部
  python3 macro_ingest.py --selftest   # 离线自检（用假 akshare，不联网）
"""
import os, re, sys, json, time, sqlite3, traceback
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "macro.db")
CFG_PATH = os.path.join(HERE, "macro_config", "indicators.json")
CN_TZ = timezone(timedelta(hours=8))
HS300_START = "19970101"


# ---------- 通用工具 ----------
def to_f(v):
    try:
        f = float(str(v).replace(",", "").replace("%", ""))
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def norm_date(s, monthly):
    """各种日期写法 → 'YYYY-MM'（月度）或 'YYYY-MM-DD'（日度）。"""
    t = str(s).strip()
    m = re.match(r"^(\d{4})[-/年.]?(\d{1,2})(?:[-/月.]?(\d{1,2}))?", t)
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    if not 1 <= mo <= 12:
        return None
    if monthly:
        return f"{y:04d}-{mo:02d}"
    d = int(m.group(3) or 1)
    return f"{y:04d}-{mo:02d}-{d:02d}"


def find_col(df, *must, any_of=()):
    for c in df.columns:
        s = str(c)
        if all(k in s for k in must) and (not any_of or any(k in s for k in any_of)):
            return c
    return None


def pairs_from(df, dcol, vcol, monthly):
    out = {}
    for d, v in df[[dcol, vcol]].values.tolist():
        nd, nv = norm_date(d, monthly), to_f(v)
        if nd and nv is not None:
            out[nd] = nv
    return out


def get_ak():
    import akshare as ak
    return ak


# ---------- 取数适配器：返回 {date: value} ----------
def ad_jin10(ind, ctx):
    """金十口径（CPI/PPI/PMI 年率等）：列 商品/日期/今值/预测值/前值。今值为空的行（未公布的预告）自动丢弃。"""
    df = getattr(get_ak(), ind["params"]["fn"])()
    dcol = find_col(df, "日期") or df.columns[1]
    vcol = find_col(df, "今值") or df.columns[2]
    return pairs_from(df, dcol, vcol, True)


def ad_money_supply(ind, ctx):
    """东方财富货币供应量：M1/M2 同比。"""
    df = get_ak().macro_china_money_supply()
    want = ind["params"]["col"]                       # "M1同比" / "M2同比"
    tag = "M1" if want.startswith("M1") else "M2"
    vcol = find_col(df, tag, "同比")
    dcol = find_col(df, "月份") or find_col(df, "日期") or df.columns[0]
    if vcol is None:
        raise RuntimeError(f"找不到 {want} 列；实际列：{list(df.columns)}")
    return pairs_from(df, dcol, vcol, True)


def ad_new_house(ind, ctx):
    """一线城市新建商品住宅价格指数同比：对每城调 macro_china_new_house_price，按月取简单平均。"""
    ak = get_ak()
    cities, want = ind["params"]["cities"], ind["params"].get("col", "同比")
    acc = {}
    for i in range(0, len(cities), 2):
        a, b = cities[i], cities[min(i + 1, len(cities) - 1)]
        df = ak.macro_china_new_house_price(city_first=a, city_second=b)
        ccol = find_col(df, "城市")
        dcol = find_col(df, "日期") or df.columns[0]
        vcol = find_col(df, "新建商品住宅", want) or find_col(df, "新建", want) or find_col(df, want)
        if vcol is None:
            raise RuntimeError(f"找不到新房价列；实际列：{list(df.columns)}")
        for city in {a, b}:
            sub = df[df[ccol] == city] if ccol else df
            for d, v in pairs_from(sub, dcol, vcol, True).items():
                acc.setdefault(d, {})[city] = round(v - 100.0, 2)      # 接口给的是指数（上年同月=100），转成同比涨跌幅%
    need = len(set(cities))
    return {d: round(sum(m.values()) / len(m), 2) for d, m in acc.items() if len(m) == need}


def ad_usdcnh(ind, ctx):
    df = get_ak().forex_hist_em(symbol="USDCNH")
    dcol = find_col(df, "日期") or df.columns[0]
    vcol = find_col(df, "最新价") or find_col(df, "收盘") or df.columns[2]
    return pairs_from(df, dcol, vcol, False)


_BOND_CACHE = {}


def ad_bond_us_cn(ind, ctx):
    if "df" not in _BOND_CACHE:
        _BOND_CACHE["df"] = get_ak().bond_zh_us_rate(start_date="20000101")
    df = _BOND_CACHE["df"]
    vcol = find_col(df, ind["params"]["col"])
    if vcol is None:
        raise RuntimeError(f"找不到列 {ind['params']['col']}；实际列：{list(df.columns)}")
    return pairs_from(df, find_col(df, "日期") or df.columns[0], vcol, False)


def ts_pro():
    tok = os.environ.get("TUSHARE_TOKEN")
    if not tok:
        raise RuntimeError("无 TUSHARE_TOKEN")
    import tushare as ts
    return ts.pro_api(tok)


def ad_tushare_cn(ind, ctx):
    """Tushare 宏观表：cn_cpi / cn_ppi / cn_pmi / cn_m（国家统计局口径，月度，按 month=YYYYMM）。"""
    fn, col = ind["params"]["fn"], ind["params"]["col"]
    df = getattr(ts_pro(), fn)(start_m="199001", end_m=datetime.now(CN_TZ).strftime("%Y%m"))
    if df is None or not len(df):
        raise RuntimeError(f"{fn} 返回空")
    if col not in df.columns:
        raise RuntimeError(f"{fn} 无列 {col}；实际列：{list(df.columns)[:12]}")
    return pairs_from(df, "month", col, True)


def ad_tushare_sf_stock_yoy(ind, ctx):
    """Tushare sf_month 的 stk_endval（社融存量，万亿元）→ 同比%。"""
    df = ts_pro().sf_month(start_m="201501", end_m=datetime.now(CN_TZ).strftime("%Y%m"))
    if df is None or not len(df) or "stk_endval" not in df.columns:
        raise RuntimeError("sf_month 无 stk_endval")
    st = pairs_from(df, "month", "stk_endval", True)
    out = {}
    for k, v in st.items():
        pk = f"{int(k[:4]) - 1:04d}-{k[5:]}"
        if pk in st and st[pk]:
            out[k] = round((v / st[pk] - 1) * 100, 2)
    return out


def ad_gdzctz(ind, ctx):
    """东方财富：城镇固定资产投资，当月同比。"""
    df = get_ak().macro_china_gdzctz()
    return pairs_from(df, find_col(df, "月份") or df.columns[0], find_col(df, "同比") or df.columns[2], True)


# ---- 国家统计局发布稿解析（房地产开发投资 / 商品房销售面积 / 工业企业利润）----
NBS_LIST = "https://www.stats.gov.cn/sj/zxfb/"
NBS_MIN_YEAR = 2019
_NBS = {}


def http_get(url, timeout=30):
    from urllib.request import Request, urlopen
    raw = urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124 Safari/537.36"}), timeout=timeout).read()
    return raw.decode("utf-8", "ignore")


def _sgn(word, num):
    return -float(num) if word in ("下降", "减少", "下滑") else float(num)


def parse_nbs_title(title):
    """→ (kind, year, month) 或 None。kind: 'property' | 'profit'。"""
    m = re.search(r"(\d{4})年1[—\-–](\d{1,2})月份全国房地产市场基本情况", title)
    if m:
        return "property", int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d{4})年1[—\-–](\d{1,2})月份全国规模以上工业企业利润", title)
    if m:
        return "profit", int(m.group(1)), int(m.group(2))
    return None


def parse_nbs_body(kind, text, month):
    out = {}
    if kind == "property":
        m = re.search(r"全国房地产开发投资[\d.,]+亿元[，,]\s*同比(增长|下降)([\d.]+)%", text)
        if m:
            out["re_invest"] = _sgn(m.group(1), m.group(2))
        m = re.search(r"商品房销售面积[\d.,]+万平方米[，,]\s*同比(增长|下降)([\d.]+)%", text)
        if m:
            out["re_sales"] = _sgn(m.group(1), m.group(2))
    else:
        m = re.search(r"利润总额[\d.,]+亿元[，,]\s*同比(增长|下降)([\d.]+)%", text)
        if m:
            out["profit_ytd"] = _sgn(m.group(1), m.group(2))
        m = re.search(rf"(?<![—\-\d]){month}月份[，,]\s*规模以上工业企业(?:实现)?利润同比(增长|下降)([\d.]+)%", text)
        if m:
            out["profit_m"] = _sgn(m.group(1), m.group(2))
    return out


def nbs_scrape(ctx):
    known = ctx.get("known", {})                       # {key: set('YYYY-MM')}，已入库的月份不再抓正文
    res = {k: {} for k in ("re_invest", "re_sales", "profit_ytd", "profit_m")}
    incremental = any(known.get(k) for k in res)
    for pg in range(0, 160):
        url = NBS_LIST + ("" if pg == 0 else f"index_{pg}.html")
        try:
            html = http_get(url)
        except Exception as ex:  # noqa: BLE001
            if pg == 0:
                raise RuntimeError(f"统计局列表页不可达：{str(ex)[:100]}")
            break
        from urllib.parse import urljoin
        items = []
        for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", html, re.S):
            attrs, inner = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
            h = re.search(r'href="([^"]+)"', attrs)
            if not h or not re.search(r"/t(\d{8})_\d+\.html", h.group(1)):
                continue
            t = re.search(r'title="([^"]+)"', attrs)
            items.append((urljoin(url, h.group(1)), (t.group(1) if t else inner).strip(), re.search(r"/t(\d{4})\d{4}_", h.group(1)).group(1)))
        if not items:
            break
        fetched = 0
        for link, title, yr in items:
            pt = parse_nbs_title(title)
            if not pt:
                continue
            kind, y, mo = pt
            if y < NBS_MIN_YEAR:
                continue
            ym = f"{y:04d}-{mo:02d}"
            keys = ("re_invest", "re_sales") if kind == "property" else ("profit_ytd", "profit_m")
            if all(ym in known.get(k, ()) for k in keys):
                continue
            try:
                body = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", http_get(link)))
            except Exception:  # noqa: BLE001
                continue
            got = parse_nbs_body(kind, body, mo)
            if kind == "profit" and "profit_ytd" not in got:           # 标题里也带累计同比
                tm = re.search(r"企业利润(增长|下降)([\d.]+)%", title)
                if tm:
                    got["profit_ytd"] = _sgn(tm.group(1), tm.group(2))
            for k, v in got.items():
                res[k][ym] = v
            fetched += 1
            time.sleep(0.3)
        if all(int(i[2]) < NBS_MIN_YEAR for i in items):
            break
        if incremental and fetched == 0 and pg >= 2 and not any(parse_nbs_title(t) and (parse_nbs_title(t)[1] >= datetime.now(CN_TZ).year - 1) for _, t, _ in items):
            break
        time.sleep(0.3)
    return res


def ad_nbs_release(ind, ctx):
    if "res" not in _NBS:
        _NBS["res"] = nbs_scrape(ctx)
    r = _NBS["res"].get(ind["params"]["key"], {})
    if not r:
        raise RuntimeError("统计局发布稿解析到 0 行（页面结构可能变了）")
    return r


ADAPTERS = {"jin10": ad_jin10, "money_supply": ad_money_supply, "tushare_cn": ad_tushare_cn, "tushare_sf_stock_yoy": ad_tushare_sf_stock_yoy,
            "gdzctz": ad_gdzctz, "nbs_release": ad_nbs_release, "new_house": ad_new_house, "usdcnh": ad_usdcnh, "bond_us_cn": ad_bond_us_cn}


# ---------- 沪深300 月末收盘（叠加用）----------
def fetch_hs300_monthly():
    tok = os.environ.get("TUSHARE_TOKEN")
    if not tok:
        raise RuntimeError("无 TUSHARE_TOKEN")
    import tushare as ts
    pro = ts.pro_api(tok)
    out, start = {}, HS300_START
    # 沪深300 2005 年起；分段取，避免单次行数上限
    for y0 in range(2005, datetime.now().year + 1, 5):
        df = pro.index_daily(ts_code="000300.SH", start_date=f"{y0}0101", end_date=f"{min(y0 + 4, datetime.now().year)}1231",
                             fields="trade_date,close")
        if df is None or not len(df):
            continue
        for d, c in df[["trade_date", "close"]].values.tolist():
            ym = norm_date(d, True)
            if ym and (ym not in out or str(d) > out[ym][0]):
                out[ym] = (str(d), float(c))
    return {k: round(v[1], 2) for k, v in out.items()}


# ---------- 落库 ----------
def db_open():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.executescript("""
      CREATE TABLE IF NOT EXISTS macro_series(key TEXT, date TEXT, value REAL, PRIMARY KEY(key, date));
      CREATE TABLE IF NOT EXISTS macro_log(key TEXT, ts TEXT, status TEXT, n INTEGER, msg TEXT);
    """)
    return c


def save(conn, key, data):
    conn.executemany("INSERT INTO macro_series(key,date,value) VALUES(?,?,?) ON CONFLICT(key,date) DO UPDATE SET value=excluded.value",
                     [(key, d, v) for d, v in data.items()])
    conn.commit()


def log(conn, key, status, n, msg=""):
    conn.execute("INSERT INTO macro_log VALUES(?,?,?,?,?)", (key, datetime.now(CN_TZ).isoformat(timespec="seconds"), status, n, msg[:300]))
    conn.commit()


def known_months(conn):
    out = {}
    for k, d in conn.execute("SELECT key,date FROM macro_series WHERE key IN ('re_invest','re_sales','profit_ytd','profit_m')"):
        out.setdefault(k, set()).add(d)
    return out


def main():
    cfg = json.load(open(CFG_PATH, encoding="utf-8"))
    conn = db_open()
    ctx = {"known": known_months(conn)}
    ok = fail = 0
    for ind in cfg["indicators"]:
        k = ind["key"]
        merged, notes = {}, []
        for src in ind["sources"]:                       # 后面的来源覆盖前面的（把更权威/更新的放后面）
            try:
                d = ADAPTERS[src["adapter"]](dict(ind, params=src.get("params", {})), ctx)
                merged.update(d)
                notes.append(f"{src['adapter']}✓{len(d)}")
            except Exception as ex:  # noqa: BLE001
                notes.append(f"{src['adapter']}✗({str(ex)[:70]})")
        if merged:
            save(conn, k, merged)
            log(conn, k, "ok", len(merged), "; ".join(notes))
            ds = sorted(merged)
            print(f"  ✓ {ind['name']:<14} {len(merged):>5} 点  {ds[0]} ~ {ds[-1]}  最新 {merged[ds[-1]]}   [{'; '.join(notes)}]")
            ok += 1
        else:
            fail += 1
            log(conn, k, "failed", 0, "; ".join(notes))
            print(f"  ⚠ {ind['name']:<14} 失败：{'; '.join(notes)}")
    try:
        h = fetch_hs300_monthly()
        save(conn, "hs300_close", h)
        log(conn, "hs300_close", "ok", len(h))
        print(f"  ✓ 沪深300月末收盘 {len(h)} 点")
        ok += 1
    except Exception as ex:  # noqa: BLE001
        fail += 1
        log(conn, "hs300_close", "failed", 0, str(ex))
        print(f"  ⚠ 沪深300叠加数据失败（不影响其它）：{str(ex)[:150]}")
    print(f"完成：成功 {ok} 项，失败 {fail} 项")
    conn.close()
    return 0


# ---------- 离线自检 ----------
def selftest():
    import types, tempfile
    import pandas as pd
    global DB_PATH, http_get
    DB_PATH = os.path.join(tempfile.mkdtemp(), "macro.db")
    months = [f"{y}-{m:02d}-01" for y in range(2018, 2027) for m in range(1, 13)]
    yms = [m[:4] + m[5:7] for m in months]
    fake = types.ModuleType("akshare")
    fake.macro_china_cpi_yearly = lambda: pd.DataFrame({"商品": "CPI", "日期": months, "今值": [1.0 + i * 0.01 for i in range(len(months))], "预测值": None, "前值": None})
    fake.macro_china_ppi_yearly = lambda: pd.DataFrame({"商品": "PPI", "日期": months, "今值": [-2.0 + i * 0.05 for i in range(len(months))], "预测值": None, "前值": None})
    fake.macro_china_pmi_yearly = lambda: pd.DataFrame({"商品": "PMI", "日期": months, "今值": [49.5] * len(months), "预测值": None, "前值": None})
    ym = [m[:4] + "年" + m[5:7] + "月份" for m in months]
    fake.macro_china_money_supply = lambda: pd.DataFrame({"月份": ym, "货币和准货币(M2)-数量(亿元)": 1, "货币和准货币(M2)-同比增长": [8.0] * len(ym), "货币(M1)-数量(亿元)": 1, "货币(M1)-同比增长": [4.0] * len(ym)})
    fake.macro_china_new_house_price = lambda city_first, city_second: pd.DataFrame(
        [{"日期": m, "城市": c, "新建商品住宅价格指数-同比": 101.5, "新建商品住宅价格指数-环比": 100.1} for m in months for c in (city_first, city_second)])
    fake.macro_china_gdzctz = lambda: pd.DataFrame({"月份": ym[::-1], "当月": 1.0, "同比增长": [-9.69] + [1.0] * (len(ym) - 1), "环比增长": 0, "自年初累计": 1})
    fake.forex_hist_em = lambda symbol: pd.DataFrame({"日期": ["2026-11-03", "2026-11-04"], "今开": 7.1, "最新价": [7.11, 7.12]})
    fake.bond_zh_us_rate = lambda start_date: pd.DataFrame({"日期": ["2026-11-03", "2026-11-04"], "中国国债收益率10年": [1.8, float("nan")], "美国国债收益率10年": [4.1, 4.2]})
    sys.modules["akshare"] = fake

    class FakePro:
        def cn_cpi(self, start_m, end_m): return pd.DataFrame({"month": yms[-24:], "nt_yoy": [0.9] * 24})
        def cn_ppi(self, start_m, end_m): return pd.DataFrame({"month": yms[-24:], "ppi_yoy": [5.0] * 24})
        def cn_pmi(self, start_m, end_m): return pd.DataFrame({"month": yms[-24:], "pmi010000": [50.2] * 24})
        def cn_m(self, start_m, end_m): return pd.DataFrame({"month": yms[-24:], "m1_yoy": [3.2] * 24, "m2_yoy": [7.2] * 24})
        def sf_month(self, start_m, end_m): return pd.DataFrame({"month": yms[::-1], "inc_month": 1.0, "inc_cumval": 1.0, "stk_endval": [400.0 + i for i in range(len(yms))][::-1]})
        def index_daily(self, ts_code, start_date, end_date, fields=None):
            return pd.DataFrame({"trade_date": ["20260930", "20261030"], "close": [4600.0, 4700.0]})
    tsm = types.ModuleType("tushare"); tsm.pro_api = lambda tok: FakePro(); sys.modules["tushare"] = tsm
    os.environ["TUSHARE_TOKEN"] = "x"

    def art(txt): return "<html><body><p>" + txt + "</p></body></html>"
    pages = {
        NBS_LIST: '<a href="./202611/t20261103_1.html" title="2026年10月下旬流通领域重要生产资料市场价格变动情况">x</a>'
                  '<a href="./202610/t20261027_2.html" title="2026年1—9月份全国规模以上工业企业利润增长15.5%">y</a>'
                  '<a href="./202610/t20261019_3.html">2026年1—9月份全国房地产市场基本情况</a>',
        NBS_LIST + "index_1.html": '<a href="./202309/t20230915_4.html">2023年1—8月份全国房地产市场基本情况</a>'
                                   '<a href="./201801/t20180120_5.html" title="2018年1—12月份全国房地产市场基本情况">z</a>',
        "https://www.stats.gov.cn/sj/zxfb/202610/t20261027_2.html": art("1—9月份，全国规模以上工业企业实现利润总额60337.4亿元，同比增长15.5%（按可比口径计算）。</p><p>9月份，规模以上工业企业利润同比增长14.4%。"),
        "https://www.stats.gov.cn/sj/zxfb/202610/t20261019_3.html": art("1—9月份，全国房地产开发投资53364亿元，同比下降20.6%（按可比口径计算）；其中，住宅投资41125亿元，下降20.5%。 1—9月份，新建商品房销售面积56483万平方米，同比下降13.2%；其中住宅销售面积下降13.8%。"),
        "https://www.stats.gov.cn/sj/zxfb/202309/t20230915_4.html": art("1—8月份，全国房地产开发投资76900亿元，同比下降8.8%。商品房销售面积74000万平方米，同比下降7.1%。"),
    }

    def fake_http(url, timeout=30):
        if url in pages: return pages[url]
        raise OSError("404 " + url)
    http_get = fake_http
    main()
    c = sqlite3.connect(DB_PATH)
    q = lambda s_: c.execute(s_).fetchone()[0]
    assert q("SELECT value FROM macro_series WHERE key='cpi_yoy' ORDER BY date DESC LIMIT 1") == 0.9          # Tushare 覆盖金十
    assert q("SELECT COUNT(*) FROM macro_series WHERE key='cpi_yoy'") == len(months)                         # 金十补长历史
    assert q("SELECT value FROM macro_series WHERE key='m2_yoy' ORDER BY date DESC LIMIT 1") == 7.2
    assert q("SELECT COUNT(*) FROM macro_series WHERE key='shrz_yoy'") > 50
    assert q("SELECT value FROM macro_series WHERE key='house_t1' LIMIT 1") == 1.5
    assert q("SELECT value FROM macro_series WHERE key='fai_yoy' AND date='2026-12'") == -9.69
    assert q("SELECT value FROM macro_series WHERE key='re_invest' AND date='2026-09'") == -20.6
    assert q("SELECT value FROM macro_series WHERE key='re_sales' AND date='2026-09'") == -13.2
    assert q("SELECT value FROM macro_series WHERE key='re_invest' AND date='2023-08'") == -8.8
    assert q("SELECT value FROM macro_series WHERE key='profit_ytd' AND date='2026-09'") == 15.5
    assert q("SELECT value FROM macro_series WHERE key='profit_m' AND date='2026-09'") == 14.4
    assert q("SELECT COUNT(*) FROM macro_series WHERE key='re_invest'") == 2                                  # 2018 年的被 NBS_MIN_YEAR 过滤
    assert q("SELECT COUNT(*) FROM macro_series WHERE key='cn10y'") == 1                                      # NaN 被丢弃
    assert q("SELECT COUNT(*) FROM macro_log WHERE status='failed'") == 0
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        sys.exit(main())
