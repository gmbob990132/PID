#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""煤炭探测 v5（只读）：秦皇岛 / 黄陵 两个页面背后的 datasql.php 接口。
1) 取页面 HTML，找出页面脚本里真实调用的 datasql 地址和列数；
2) 用页面自带的 data/name 参数直接试接口，看历史有多长、是日度还是周度。"""
import re, sys, json
from urllib.request import Request, urlopen

PAGES = {
    "秦皇岛": ("https://www.cctd.com.cn/index.php?m=content&c=index&a=lists&catid=454&data=CCTD%C7%D8%BB%CA%B5%BA%B6%AF%C1%A6%C3%BA%BC%DB%B8%F1&name=CCTD%C7%D8%BB%CA%B5%BA%B6%AF%C1%A6%C3%BA%BC%DB%B8%F1",
               "CCTD%C7%D8%BB%CA%B5%BA%B6%AF%C1%A6%C3%BA%BC%DB%B8%F1", "CCTD%C7%D8%BB%CA%B5%BA%B6%AF%C1%A6%C3%BA%BC%DB%B8%F1"),
    "黄陵": ("https://www.cctd.com.cn/index.php?m=content&c=index&a=lists&catid=689&data=%BB%C6%C1%EA%D6%B8%CA%FD&name=%BB%C6%C1%EA%D6%B8%CA%FD",
             "%BB%C6%C1%EA%D6%B8%CA%FD", "%BB%C6%C1%EA%D6%B8%CA%FD"),
}
TIME = "%20where%20%20DATE_FORMAT(END_DATE,%27%Y-%m-%d%27)%20%3E=%20%27{d}%27"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
      "Accept": "application/json, text/javascript, */*; q=0.01", "X-Requested-With": "XMLHttpRequest",
      "Referer": "https://www.cctd.com.cn/"}

def cols(n):
    return "".join(f"&columns%5B{i}%5D%5Bdata%5D={i}&columns%5B{i}%5D%5Bname%5D=&columns%5B{i}%5D%5Bsearchable%5D=true"
                   f"&columns%5B{i}%5D%5Borderable%5D=false&columns%5B{i}%5D%5Bsearch%5D%5Bvalue%5D=&columns%5B{i}%5D%5Bsearch%5D%5Bregex%5D=false" for i in range(n))

def make_url(data, name, d, ncol, length):
    return (f"https://www.cctd.com.cn/datasql.php?data={data}&name={name}&time={TIME.format(d=d)}&draw=1{cols(ncol)}"
            f"&start=0&length={length}&search%5Bvalue%5D=&search%5Bregex%5D=false&extra_search=&_=1792397583313")

def fetch(url):
    r = urlopen(Request(url, headers=UA), timeout=40); raw = r.read()
    for enc in ("utf-8", "gb18030"):
        try: return r.status, raw.decode(enc)
        except UnicodeDecodeError: pass
    return r.status, raw.decode("utf-8", "ignore")

def clean(x): return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(x))).strip()

def page_scripts(html):
    print(f"  页面长度 {len(html)}；SQL 错误: {bool(re.search('SQL syntax', html))}")
    hits = [m.start() for m in re.finditer(r"datasql|ajax\s*:|columns\s*:|serverSide", html)]
    shown = 0; last = -999
    for p in hits:
        if p - last < 400: continue
        print("   ...", re.sub(r"\s+", " ", html[max(0, p - 150): p + 450]), "..."); last = p; shown += 1
        if shown >= 6: break
    if not hits: print("   页面里没找到 datasql/ajax/columns 关键字")
    print("   表头:", [clean(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", html, flags=re.S)][:14])

def show(label, text):
    print(f"  [{label}] 原文前 250 字: {text[:250]!r}")
    try: j = json.loads(text)
    except Exception as e: print("   JSON 解析失败:", e); return
    rows = j.get("data") or []
    print(f"   recordsTotal={j.get('recordsTotal')} 本次行数={len(rows)} 每行列数={len(rows[0]) if rows and isinstance(rows[0], list) else '-'}")
    for r in rows[:3]: print("   头:", [clean(c) for c in r] if isinstance(r, list) else r)
    for r in rows[-2:]: print("   尾:", [clean(c) for c in r] if isinstance(r, list) else r)
    ds = [clean(r[0]) for r in rows if isinstance(r, list) and r and re.match(r"20\d\d", clean(r[0]))]
    if ds: print(f"   日期 {min(ds)} ~ {max(ds)}，共 {len(ds)} 个，>=2025-01-01 的 {sum(d >= '2025-01-01' for d in ds)} 个")

if "--selftest" in sys.argv:
    u = make_url("A", "B", "2025-01-01", 10, 5000)
    assert u.count("columns%5B9%5D%5Bdata%5D=9") == 1 and "%27%Y-%m-%d%27" in u
    page_scripts("<script>$('#t').DataTable({serverSide:true, ajax:'datasql.php?data=x', columns:[{data:0}]});</script><th>时间</th>")
    show("selftest", json.dumps({"recordsTotal": 2, "data": [["2026-10-16", "770"], ["2025-01-03", "700"]]}))
    print("selftest OK"); sys.exit(0)

for nm, (page, data, name) in PAGES.items():
    print(f"\n{'='*20} {nm}", flush=True)
    try:
        st, html = fetch(page); print(f" 页面 HTTP {st}"); page_scripts(html)
    except Exception as e:
        print(f" 页面 ERR {type(e).__name__} {str(e)[:200]}")
    for label, d, ncol, length in (("A 浏览器同款(3列) 前10条", "-0002-11-30", 3, 10),
                                   ("B 10列 前10条", "-0002-11-30", 10, 10),
                                   ("C 3列 从2025-01-01 最多5000条", "2025-01-01", 3, 5000),
                                   ("D 10列 从2025-01-01 最多5000条", "2025-01-01", 10, 5000)):
        try:
            st, text = fetch(make_url(data, name, d, ncol, length)); print(f" HTTP {st}", end="")
            show(label, text)
        except Exception as e:
            print(f" [{label}] ERR {type(e).__name__} {str(e)[:200]}")
