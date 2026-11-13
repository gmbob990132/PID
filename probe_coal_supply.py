#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""煤炭供需探测（只读，不写库、不提交）：
  A. 焦煤 / 焦炭期货主力（akshare futures_main_sina：JM0 / J0）
  B. 港口库存 / 电厂日耗 / 可用天数：CCTD 数据栏目清单（爬站内栏目，列出全部 data=数据集，按关键字筛选后试接口）
     + akshare 里与煤炭/能源相关的函数（含 macro_china_daily_energy 沿海电厂库存日耗）
  C. 中电联（CEC）网站：找 CECI / 电煤 / 日耗 / 库存相关栏目
  D. 原煤产量、煤炭进口量：国家统计局"能源生产情况"月度发布（复用 macro_ingest 的列表爬虫）
用法：python3 probe_coal_supply.py   （每一节独立，某节失败不影响其他节）
"""
import re, sys, json, time, html as H, traceback
from urllib.parse import unquote, urljoin
from urllib.request import Request, urlopen

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
      "Accept-Language": "zh-CN,zh;q=0.9"}
KW = re.compile(r"库存|日耗|可用|电厂|港口|秦皇岛港|曹妃甸|京唐|黄骅|环渤海|榆林|小保当|红柳林|鄂尔多斯|进口|产量|调入|调出|锚地")


def get(url, timeout=30, extra=None):
    r = urlopen(Request(url, headers={**UA, **(extra or {})}), timeout=timeout)
    raw = r.read()
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "ignore")


def clean(x):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", H.unescape(str(x)))).strip()


def section(t):
    print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70, flush=True)


def guarded(fn):
    def w():
        try:
            fn()
        except Exception:  # noqa: BLE001
            print("本节异常：\n" + traceback.format_exc()[-1500:])
    return w


# ---------------- A. 焦煤 / 焦炭期货 ----------------
@guarded
def part_a():
    section("A. 焦煤 JM0 / 焦炭 J0 期货主力（akshare futures_main_sina）")
    import akshare as ak
    print("akshare 版本:", getattr(ak, "__version__", "?"))
    for sym, nm in (("JM0", "焦煤"), ("J0", "焦炭")):
        try:
            df = ak.futures_main_sina(symbol=sym)
            print(f"\n[{nm} {sym}] 行数 {len(df)}，列 {list(df.columns)}")
            print(f"  日期 {df.iloc[0, 0]} ~ {df.iloc[-1, 0]}")
            print(df.tail(3).to_string())
        except Exception as e:  # noqa: BLE001
            print(f"[{nm} {sym}] 失败：{type(e).__name__} {str(e)[:200]}")


# ---------------- B1. akshare 能源相关函数 ----------------
@guarded
def part_b_ak():
    section("B1. akshare 中与煤炭 / 能源 / 库存相关的函数")
    import akshare as ak
    names = [n for n in dir(ak) if re.search(r"coal|energy|qhd|power|electric|inventory|stock_qhd|cci|ceci", n, re.I)]
    print("函数名：", names)
    for fn in ("macro_china_daily_energy",):
        if hasattr(ak, fn):
            try:
                df = getattr(ak, fn)()
                print(f"\n[{fn}] 行数 {len(df)}，列 {list(df.columns)}")
                print(df.head(2).to_string()); print("..."); print(df.tail(4).to_string())
            except Exception as e:  # noqa: BLE001
                print(f"[{fn}] 失败：{type(e).__name__} {str(e)[:200]}")


# ---------------- B2. CCTD 栏目清单 ----------------
CCTD = "https://www.cctd.com.cn/"
TIME = "%20where%20%20DATE_FORMAT(END_DATE,%27%Y-%m-%d%27)%20%3E=%20%27{d}%27"
XHR = {"Accept": "application/json, text/javascript, */*; q=0.01", "X-Requested-With": "XMLHttpRequest", "Referer": CCTD}


def cols(n):
    return "".join(f"&columns%5B{i}%5D%5Bdata%5D={i}&columns%5B{i}%5D%5Bname%5D=&columns%5B{i}%5D%5Bsearchable%5D=true"
                   f"&columns%5B{i}%5D%5Borderable%5D=false&columns%5B{i}%5D%5Bsearch%5D%5Bvalue%5D=&columns%5B{i}%5D%5Bsearch%5D%5Bregex%5D=false"
                   for i in range(n))


def datasql(data_q, name_q, since="-0002-11-30", length=2000):
    u = (f"{CCTD}datasql.php?data={data_q}&name={name_q}&time={TIME.format(d=since)}&draw=1{cols(10)}"
         f"&start=0&length={length}&search%5Bvalue%5D=&search%5Bregex%5D=false&extra_search=&_={int(time.time()*1000)}")
    return json.loads(get(u, 40, XHR))


def dec(s):
    try:
        return unquote(s, encoding="gb18030")
    except Exception:  # noqa: BLE001
        return s


@guarded
def part_b_cctd():
    section("B2. CCTD 中国煤炭市场网：站内数据栏目清单 + 关键字命中项试接口")
    seeds = [CCTD,
             CCTD + "index.php?m=content&c=index&a=lists&catid=454",
             CCTD + "index.php?m=content&c=index&a=lists&catid=689"]
    todo, seen, datasets = list(seeds), set(), {}     # datasets[(data_q,name_q)] = (catid, 链接文字, 页面URL)
    cat_titles = {}
    while todo and len(seen) < 160:
        u = todo.pop(0)
        if u in seen:
            continue
        seen.add(u)
        try:
            page = get(u, 25)
        except Exception as e:  # noqa: BLE001
            print(f"  取页失败 {u[:90]}：{type(e).__name__}")
            continue
        for href, txt in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page, flags=re.S):
            href = urljoin(u, H.unescape(href)); t = clean(txt)
            if "cctd.com.cn" not in href:
                continue
            m = re.search(r"catid=(\d+)", href)
            if m and "a=lists" in href:
                cat_titles.setdefault(m.group(1), t)
                if href not in seen and len(todo) < 600:
                    todo.append(href)
            dm, nm = re.search(r"[?&]data=([^&#]+)", href), re.search(r"[?&]name=([^&#]+)", href)
            if dm:
                key = (dm.group(1), nm.group(1) if nm else dm.group(1))
                datasets.setdefault(key, (m.group(1) if m else "?", t, href))
        time.sleep(0.25)
    print(f"爬了 {len(seen)} 个页面，栏目 {len(cat_titles)} 个，带 data= 的数据集 {len(datasets)} 个")
    print("\n—— 全部栏目（catid: 标题）——")
    print("；".join(f"{k}:{v}" for k, v in sorted(cat_titles.items(), key=lambda x: int(x[0])) if v)[:6000])
    print("\n—— 全部数据集（catid | 链接文字 | data 参数解码）——")
    for (dq, nq), (cid, t, href) in sorted(datasets.items(), key=lambda x: x[1][0]):
        flag = "★" if KW.search(dec(dq) + t) else " "
        print(f" {flag} {cid:>5} | {t[:24]:<24} | {dec(dq)}")
    hits = [(k, v) for k, v in datasets.items() if KW.search(dec(k[0]) + v[1])]
    print(f"\n—— 关键字命中 {len(hits)} 个，逐个试接口（最多 25 个）——")
    for (dq, nq), (cid, t, href) in hits[:25]:
        print(f"\n[{dec(dq)}]（{t}，catid={cid}）")
        try:
            pg = get(href, 25)
            print("  表头:", [clean(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", pg, flags=re.S)][:14])
        except Exception as e:  # noqa: BLE001
            print("  页面表头取失败:", type(e).__name__)
        try:
            j = datasql(dq, nq)
            rows = j.get("data") or []
            print(f"  recordsTotal={j.get('recordsTotal')} 返回 {len(rows)} 行")
            for r in rows[:2]:
                print("   头:", [clean(c) for c in r][:10])
            for r in rows[-2:]:
                print("   尾:", [clean(c) for c in r][:10])
        except Exception as e:  # noqa: BLE001
            print(f"  接口失败：{type(e).__name__} {str(e)[:150]}")
        time.sleep(0.4)


# ---------------- C. 中电联 ----------------
@guarded
def part_c():
    section("C. 中电联 CEC：CECI / 电煤 / 日耗 / 库存 相关链接")
    kw = re.compile(r"CECI|电煤|燃料|日耗|库存|可用天数|采购价格指数")
    found = {}
    for start in ("https://www.cec.org.cn/", "https://cec.org.cn/"):
        try:
            page = get(start, 25)
            print(f"{start} 取到 {len(page)} 字")
        except Exception as e:  # noqa: BLE001
            print(f"{start} 失败：{type(e).__name__} {str(e)[:120]}")
            continue
        for href, txt in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page, flags=re.S):
            t = clean(txt)
            if kw.search(t) or kw.search(href):
                found.setdefault(urljoin(start, H.unescape(href)), t)
        break
    for u, t in list(found.items())[:40]:
        print(f"  {t[:40]:<40} {u}")
    for u, t in list(found.items())[:6]:
        try:
            txt = clean(get(u, 25))
            snips = [txt[max(0, m.start()-60): m.start()+120] for m in re.finditer(r"日耗|库存|可用天数|CECI", txt)][:5]
            print(f"\n[{t[:30]}] {u}\n  " + "\n  ".join(snips) if snips else f"\n[{t[:30]}] 无相关文字")
        except Exception as e:  # noqa: BLE001
            print(f"  {u} 失败：{type(e).__name__}")


# ---------------- D. 统计局 能源生产情况 ----------------
@guarded
def part_d():
    section("D. 国家统计局『能源生产情况』月度发布：原煤产量 / 进口煤炭")
    sys.argv = [a for a in sys.argv if a != "--backfill"]
    import macro_ingest as mi
    arts = {}
    for base in mi.NBS_LISTS:
        a, last_pg, last_yr, err = mi.nbs_crawl(base, 70)
        print(f"{base}：扫到第 {last_pg} 页，最早年份 {last_yr}，文章 {len(a)}，错误 {err}")
        for k, v in a.items():
            if k not in arts or len(v[1]) > len(arts[k][1]):
                arts[k] = v
    en = sorted([(k, v) for k, v in arts.items() if "能源生产" in v[1]], key=lambda x: x[0], reverse=True)
    print(f"\n标题含『能源生产』的文章 {len(en)} 篇")
    for k, (link, t) in en[:40]:
        print(f"  {k}  {t}")
    if en:
        print(f"  ……最早一篇：{en[-1][1][1]}")
    for k, (link, t) in en[:3]:
        try:
            txt = clean(mi.http_get(link))
            sents = [s for s in re.split(r"[。；]", txt) if re.search(r"原煤|进口煤", s)][:6]
            print(f"\n[{t}] {link}")
            for s in sents:
                print("  ·", s.strip()[:200])
        except Exception as e:  # noqa: BLE001
            print(f"  正文失败 {link}：{type(e).__name__}")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        assert dec("%BB%C6%C1%EA%D6%B8%CA%FD") == "黄陵指数"
        assert KW.search("秦皇岛港库存") and not KW.search("综合交易价")
        assert clean("<b>a&amp;b</b>  c") == "a&b c"
        print("selftest OK"); sys.exit(0)
    for p in (part_a, part_b_ak, part_b_cctd, part_c, part_d):
        p()
    print("\n探测结束")
