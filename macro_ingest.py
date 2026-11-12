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
import html as html_lib
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
    """东方财富离岸人民币；容易被限流，失败时退避重试（15s、45s）。"""
    last = None
    for wait in (0, 15, 45):
        if wait:
            time.sleep(wait)
        try:
            df = get_ak().forex_hist_em(symbol="USDCNH")
            dcol = find_col(df, "日期") or df.columns[0]
            vcol = find_col(df, "最新价") or find_col(df, "收盘") or df.columns[2]
            return pairs_from(df, dcol, vcol, False)
        except Exception as ex:  # noqa: BLE001
            last = ex
    raise RuntimeError(str(last)[:100])


def ad_tushare_fx(ind, ctx):
    """Tushare fx_daily（USDCNH.FXCM，取 bid_close）。单次上限 1000 行，按 3 年分段。"""
    pro = ts_pro()
    code = ind_param(ind, "ts_code", "USDCNH.FXCM")
    out, y = {}, 2018
    end_y = datetime.now(CN_TZ).year
    while y <= end_y:
        df = pro.fx_daily(ts_code=code, start_date=f"{y}0101", end_date=f"{min(y + 2, end_y)}1231")
        if df is not None and len(df):
            out.update(pairs_from(df, "trade_date", "bid_close", False))
        y += 3
    if not out:
        raise RuntimeError("fx_daily 返回空（可能积分不足）")
    return out


def ind_param(ind, k, default=None):
    return ind.get("params", {}).get(k, default)


def ad_seed_csv(ind, ctx):
    """人工种子：macro_config/seed_manual.csv（key,date,value），最低优先级，用于一次性回填历史。"""
    path = os.path.join(HERE, "macro_config", "seed_manual.csv")
    if not os.path.exists(path):
        raise RuntimeError("无 seed_manual.csv")
    out, key = {}, ind["params"]["key"]
    for line in open(path, encoding="utf-8"):
        parts = [x.strip() for x in line.strip().split(",")]
        if len(parts) >= 3 and parts[0] == key:
            d, v = norm_date(parts[1], True), to_f(parts[2])
            if d and v is not None:
                out[d] = v
    if not out:
        raise RuntimeError("种子中无此指标")
    return out


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


# ---- 国家统计局发布稿解析（房地产 / 工业利润 / CPI·PPI 最新月）----
# 文章实际路径在 /xxgk/sjfb/zxfb2020/；旧的 /sj/zxfb/ 作为备用。两处都试，谁有内容用谁。
NBS_LISTS = ["https://www.stats.gov.cn/xxgk/sjfb/zxfb2020/", "https://www.stats.gov.cn/sj/zxfb/"]
NBS_MIN_YEAR = 2019
NBS_KEYS = ("re_invest", "re_sales", "profit_ytd", "profit_m", "cpi_yoy", "ppi_yoy")
_NBS = {}


def http_get(url, timeout=30):
    from urllib.request import Request, urlopen
    raw = urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124 Safari/537.36"}), timeout=timeout).read()
    return raw.decode("utf-8", "ignore")


def _sgn(word, num):
    return -float(num) if word in ("下降", "减少", "下滑") else float(num)


def parse_nbs_title(title):
    """→ (kind, year, month, value|None)。kind: property | profit | cpi | ppi。CPI/PPI 标题自带数值。"""
    m = re.search(r"(\d{4})年1[—\-–](\d{1,2})月份全国房地产市场基本情况", title)
    if m:
        return "property", int(m.group(1)), int(m.group(2)), None
    m = re.search(r"(\d{4})年1[—\-–](\d{1,2})月份全国规模以上工业企业利润", title)
    if m:
        return "profit", int(m.group(1)), int(m.group(2)), None
    for kind, pat in (("cpi", "居民消费价格"), ("ppi", "工业生产者出厂价格")):
        m = re.search(rf"(\d{{4}})年(\d{{1,2}})月份{pat}同比(上涨|下降|持平)([\d.]*)", title)
        if m:
            v = 0.0 if m.group(3) == "持平" or not m.group(4) else _sgn(m.group(3), m.group(4))
            return kind, int(m.group(1)), int(m.group(2)), v
    return None


def parse_nbs_body(kind, text, month):
    out = {}
    if kind == "property":
        m = re.search(r"全国房地产开发投资[\d.,]+亿元[^同]{0,14}同比(增长|下降)([\d.]+)%", text)
        if m:
            out["re_invest"] = _sgn(m.group(1), m.group(2))
        m = re.search(r"商品房销售面积[\d.,]+万平方米[^同]{0,14}同比(增长|下降)([\d.]+)%", text)
        if m:
            out["re_sales"] = _sgn(m.group(1), m.group(2))
    else:
        m = re.search(r"利润总额[\d.,]+亿元[^同]{0,14}同比(增长|下降)([\d.]+)%", text)
        if m:
            out["profit_ytd"] = _sgn(m.group(1), m.group(2))
        # 单月句式多样："9月份，规模以上工业企业利润同比增长14.4%" / "8月份，…利润同比由上月下降1.5%转为增长20.4%，…"
        m = re.search(rf"(?<![—\-\d]){month}月份[，,]规模以上工业企业(?:实现)?利润[^。；]*", text)
        if m:
            sent = m.group(0)
            if "转为" in sent:
                sent = sent.split("转为", 1)[1]
            n = re.search(r"(增长|下降)([\d.]+)%", sent)
            if n:
                out["profit_m"] = _sgn(n.group(1), n.group(2))
    return out


def nbs_list_items(html, url):
    from urllib.parse import urljoin
    items = []
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", html, re.S):
        attrs, inner = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
        h = re.search(r'href="([^"]+)"', attrs)
        if not h or not re.search(r"/t(\d{8})_\d+\.html", h.group(1)):
            continue
        t = re.search(r'title="([^"]+)"', attrs)
        items.append((urljoin(url, h.group(1)), (t.group(1) if t else inner).strip(), re.search(r"/t(\d{4})\d{4}_", h.group(1)).group(1)))
    return items


def nbs_crawl(base, max_pages):
    """翻页抓一个列表栏目 → ({文章ID: (链接, 最长标题)}, 扫描页数, 最后一页最早年份, 错误)。404 视为到头。"""
    arts, last_pg, last_yr, err, fails = {}, -1, None, None, 0
    for pg in range(0, max_pages):
        url = base + ("" if pg == 0 else f"index_{pg}.html")
        html, code = None, None
        for _try in range(3):                           # 单页偶发失败重试
            try:
                html = http_get(url)
                break
            except Exception as ex:  # noqa: BLE001
                code = getattr(ex, "code", None)
                err = err or str(ex)[:100]
                if code == 404:
                    break
                time.sleep(2)
        if html is None:
            if code == 404 or pg == 0:
                break                                   # 到头 / 首页都不可达
            fails += 1
            if fails >= 3:
                break
            continue
        fails = 0
        items = nbs_list_items(html, url)
        if not items:
            break
        last_pg, last_yr = pg, min(int(i[2]) for i in items)
        for link, title, yr in items:                   # 同一篇文章在页面里常出现多个锚点（有的标题被截断），取最长标题
            aid = re.search(r"/(t\d{8}_\d+)\.html", link).group(1)
            if aid not in arts or len(title) > len(arts[aid][1]):
                arts[aid] = (link, title)
        if last_yr < NBS_MIN_YEAR:
            break
        time.sleep(0.3)
    return arts, last_pg, last_yr, err


def nbs_scrape(ctx):
    known = ctx.get("known", {})                       # {key: set('YYYY-MM')}，已入库的月份不再抓正文
    res = {k: {} for k in NBS_KEYS}
    # 已有 ≥24 个月历史才算"增量"，只扫最近 6 页；否则全量回填。
    # 两个栏目互补：zxfb2020 约保留最近 2 年（约 50 页）；旧栏目 /sj/zxfb/ 约 67 页，历史更长。
    incremental = "--backfill" not in sys.argv and all(len(known.get(k, ())) >= 24 for k in ("re_invest", "re_sales", "profit_ytd", "profit_m"))
    max_pages = 6 if incremental else 120
    allarts, errs = {}, []
    for base in NBS_LISTS:
        arts, lp, ly, err = nbs_crawl(base, max_pages)
        print(f"    · 统计局列表 {base}：{len(arts)} 篇，扫到第 {lp} 页（最早年份 {ly}）")
        if not arts and err:
            errs.append(err)
        for aid, v in arts.items():
            if aid not in allarts or len(v[1]) > len(allarts[aid][1]):
                allarts[aid] = v if aid not in allarts else (allarts[aid][0], v[1])
    if not allarts:
        raise RuntimeError(f"统计局列表页不可达：{(errs or ['无内容'])[0]}")
    bad, nfetch = [], 0
    hits = {"property": [], "profit": []}
    for aid, (link, title) in sorted(allarts.items(), reverse=True):
        pt = parse_nbs_title(title)
        if not pt:
            continue
        kind, y, mo, val = pt
        if y < NBS_MIN_YEAR:
            continue
        ym = f"{y:04d}-{mo:02d}"
        if kind in ("cpi", "ppi"):
            res[f"{kind}_yoy"][ym] = val                  # 标题即数值，不抓正文
            continue
        hits[kind].append(ym)
        keys = ("re_invest", "re_sales") if kind == "property" else ("profit_ytd", "profit_m")
        if all(ym in known.get(k, ()) for k in keys):
            continue
        try:
            body = re.sub(r"\s+", "", html_lib.unescape(re.sub(r"<[^>]+>", "", http_get(link))))
        except Exception:  # noqa: BLE001
            bad.append((title, "正文抓取失败"))
            continue
        nfetch += 1
        got = parse_nbs_body(kind, body, mo)
        want = 2
        if len(got) < want and mo != 2 and len(bad) < 6:      # 1—2 月合并发布、无单月数据，不算失败
            i = body.find("分月看")
            if i < 0:
                i = max(body.find(f"{mo}月份，规模"), body.find("房地产开发投资"), 0)
            bad.append((title, f"解析到 {sorted(got)}；正文片段：{body[i:i + 130]}"))
        if kind == "profit" and "profit_ytd" not in got:           # 标题里也带累计同比
            tm = re.search(r"企业利润(增长|下降)([\d.]+)%", title)
            if tm:
                got["profit_ytd"] = _sgn(tm.group(1), tm.group(2))
        for k, v in got.items():
            res[k][ym] = v
        time.sleep(0.3)
    print(f"    · 库中已有：" + "，".join(f"{k} {len(v)}" for k, v in known.items()))
    print(f"    · 抓取正文 {nfetch} 篇，解析不全 {len(bad)} 篇（最多显示 6 条）")
    for t, why in bad[:6]:
        print(f"      - {t[:30]} → {why}")
    print(f"    · 标题命中：地产 {len(hits['property'])} 篇 {sorted(hits['property'])[:3]}…{sorted(hits['property'])[-2:]}；利润 {len(hits['profit'])} 篇 {sorted(hits['profit'])[:3]}…{sorted(hits['profit'])[-2:]}")
    res["_hits"] = {"re_invest": len(hits["property"]), "re_sales": len(hits["property"]), "profit_ytd": len(hits["profit"]), "profit_m": len(hits["profit"]), "cpi_yoy": len(res["cpi_yoy"]), "ppi_yoy": len(res["ppi_yoy"])}
    print(f"    · 解析结果：CPI {len(res['cpi_yoy'])} / PPI {len(res['ppi_yoy'])} / 地产投资 {len(res['re_invest'])} / 销售面积 {len(res['re_sales'])} / 利润累计 {len(res['profit_ytd'])} / 利润当月 {len(res['profit_m'])}")
    return res


def ad_nbs_release(ind, ctx):
    if "res" not in _NBS:
        _NBS["res"] = nbs_scrape(ctx)
    key = ind["params"]["key"]
    r = _NBS["res"].get(key, {})
    if not r and _NBS["res"]["_hits"].get(key, 0) == 0:
        raise RuntimeError("统计局列表里没有匹配的标题（页面结构可能变了）")
    return r                                       # 有命中但都已入库 → 本次无新增，不算失败


ADAPTERS = {"jin10": ad_jin10, "money_supply": ad_money_supply, "tushare_cn": ad_tushare_cn, "tushare_sf_stock_yoy": ad_tushare_sf_stock_yoy,
            "gdzctz": ad_gdzctz, "nbs_release": ad_nbs_release, "tushare_fx": ad_tushare_fx, "seed_csv": ad_seed_csv, "new_house": ad_new_house, "usdcnh": ad_usdcnh, "bond_us_cn": ad_bond_us_cn}


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
        def fx_daily(self, ts_code, start_date, end_date): return pd.DataFrame({"trade_date": ["20260210", "20261104"], "bid_close": [7.0, 7.11]})
        def index_daily(self, ts_code, start_date, end_date, fields=None):
            return pd.DataFrame({"trade_date": ["20260930", "20261030"], "close": [4600.0, 4700.0]})
    tsm = types.ModuleType("tushare"); tsm.pro_api = lambda tok: FakePro(); sys.modules["tushare"] = tsm
    os.environ["TUSHARE_TOKEN"] = "x"

    def art(txt): return "<html><body><p>" + txt + "</p></body></html>"
    pages = {
        NBS_LISTS[0]: '<a href="./202611/t20261109_9.html" title="2026年10月份居民消费价格同比上涨0.7%">c</a>'
                  '<a href="./202611/t20261109_8.html" title="2026年10月份工业生产者出厂价格同比上涨5.7%">p</a>'
                  '<a href="./202611/t20261103_1.html" title="2026年10月下旬流通领域重要生产资料市场价格变动情况">x</a>'
                  '<a href="./202610/t20261027_2.html" title="2026年1—9月份全国规模以上工业企业利润增长15.5%">y</a>'
                  '<a href="./202610/t20261019_3.html">2026年1—9月份全国房地产市场基本情况</a>',
        NBS_LISTS[0] + "index_1.html": '<a href="./202309/t20230915_4.html">2023年1—8月份全国房地产市场基本情况</a>'
                                   '<a href="./201801/t20180120_5.html" title="2018年1—12月份全国房地产市场基本情况">z</a>',
        "https://www.stats.gov.cn/xxgk/sjfb/zxfb2020/202610/t20261027_2.html": art("1—9月份，全国规模以上工业企业实现利润总额60337.4亿元，同比增长15.5%（按可比口径计算）。</p><p>9月份，规模以上工业企业利润同比由上月下降1.5%转为增长14.4%。"),
        "https://www.stats.gov.cn/xxgk/sjfb/zxfb2020/202610/t20261019_3.html": art("1—9月份，全国房地产开发投资53364亿元，&nbsp;同比下降20.6%（按可比口径计算）；其中，住宅投资41125亿元，下降20.5%。 1—9月份，新建商品房销售面积56483万平方米，同比下降13.2%；其中住宅销售面积下降13.8%。"),
        "https://www.stats.gov.cn/xxgk/sjfb/zxfb2020/202309/t20230915_4.html": art("1—8月份，全国房地产开发投资76900亿元，同比下降8.8%。商品房销售面积74000万平方米，同比下降7.1%。"),
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
    assert q("SELECT COUNT(*) FROM macro_series WHERE key='re_invest'") == 3                                  # 2018 年的被 NBS_MIN_YEAR 过滤
    assert q("SELECT COUNT(*) FROM macro_series WHERE key='cn10y'") == 1                                      # NaN 被丢弃
    assert q("SELECT value FROM macro_series WHERE key='cpi_yoy' AND date='2026-10'") == 0.7                  # 统计局标题补最新月
    assert q("SELECT value FROM macro_series WHERE key='ppi_yoy' AND date='2026-10'") == 5.7
    assert q("SELECT COUNT(*) FROM macro_series WHERE key='usdcnh'") >= 3                                    # 东财 + Tushare 合并
    assert q("SELECT value FROM macro_series WHERE key='re_invest' AND date='2020-03'") == -7.7              # 种子回填
    assert q("SELECT COUNT(*) FROM macro_log WHERE status='failed'") == 0
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        sys.exit(main())
