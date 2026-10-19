#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""煤炭探测 v4（只读）：CCTD 产地价格指数的 datasql.php 接口（山西/陕西/蒙西/蒙东）。
看：能否取到 JSON、一共多少条、字段含义、能否从 2025-01-01 起一次取全。"""
import re, sys, json
from urllib.request import Request, urlopen

BASE = "https://www.cctd.com.cn/datasql.php"
NAMES = {"山西": "%C9%BD%CE%F7", "陕西": "%C9%C2%CE%F7", "蒙西": "%C3%C9%CE%F7", "蒙东": "%C3%C9%B6%AB"}
COLS = "".join(
    f"&columns%5B{i}%5D%5Bdata%5D={i}&columns%5B{i}%5D%5Bname%5D=&columns%5B{i}%5D%5Bsearchable%5D=true"
    f"&columns%5B{i}%5D%5Borderable%5D=false&columns%5B{i}%5D%5Bsearch%5D%5Bvalue%5D=&columns%5B{i}%5D%5Bsearch%5D%5Bregex%5D=false"
    for i in range(3))
TIME = "%20where%20%20DATE_FORMAT(END_DATE,%27%Y-%m-%d%27)%20%3E=%20%27{d}%27"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
      "Accept": "application/json, text/javascript, */*; q=0.01", "X-Requested-With": "XMLHttpRequest",
      "Referer": "https://www.cctd.com.cn/"}

def make_url(gbk, d, start, length):
    return (f"{BASE}?data=CCTD{gbk}%BC%DB%B8%F1%D6%B8%CA%FD&name=CCTD%BC%DB%B8%F1%D6%B8%CA%FD&time={TIME.format(d=d)}"
            f"&draw=1{COLS}&start={start}&length={length}&search%5Bvalue%5D=&search%5Bregex%5D=false&extra_search=&_=1792396968346")

def fetch(url):
    r = urlopen(Request(url, headers=UA), timeout=40); raw = r.read()
    for enc in ("utf-8", "gb18030"):
        try: return r.status, raw.decode(enc)
        except UnicodeDecodeError: pass
    return r.status, raw.decode("utf-8", "ignore")

def clean(x): return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(x))).strip()

def show(label, text):
    print(f"  [{label}] 原文前 300 字: {text[:300]!r}")
    try:
        j = json.loads(text)
    except Exception as e:
        print("   JSON 解析失败:", e); return None
    rows = j.get("data") or []
    print(f"   keys={list(j.keys())} recordsTotal={j.get('recordsTotal')} recordsFiltered={j.get('recordsFiltered')} 本次行数={len(rows)}")
    for r in rows[:3]: print("   头:", [clean(c) for c in r] if isinstance(r, list) else r)
    for r in rows[-2:]: print("   尾:", [clean(c) for c in r] if isinstance(r, list) else r)
    ds = [clean(r[0]) for r in rows if isinstance(r, list) and r and re.match(r"20\d\d", clean(r[0]))]
    if ds: print(f"   日期范围 {min(ds)} ~ {max(ds)}，共 {len(ds)} 个日期，其中 >=2025-01-01 的 {sum(d >= '2025-01-01' for d in ds)} 个")
    return j

if "--selftest" in sys.argv:
    u = make_url(NAMES["山西"], "2025-01-01", 0, 10)
    assert u.count("columns%5B2%5D%5Bdata%5D=2") == 1 and "%27%Y-%m-%d%27" in u and "start=0&length=10" in u
    j = show("selftest", json.dumps({"draw": 1, "recordsTotal": 3, "recordsFiltered": 3,
                                      "data": [["2026-10-16", "<b>751</b>", "35"], ["2026-10-09", "716", "-5"], ["2025-01-03", "700", "0"]]}))
    assert len(j["data"]) == 3; print("selftest OK"); sys.exit(0)

for nm, gbk in NAMES.items():
    print(f"\n{'='*20} {nm}", flush=True)
    for label, d, length in (("A 浏览器同款 前10条", "-0002-11-30", 10), ("B 从2025-01-01起 最多5000条", "2025-01-01", 5000)):
        try:
            st, text = fetch(make_url(gbk, d, 0, length)); print(f"  HTTP {st}", end="")
            show(label, text)
        except Exception as e:
            print(f"  [{label}] ERR {type(e).__name__} {str(e)[:200]}")
