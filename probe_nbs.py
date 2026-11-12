import re, urllib.request
BASES = ["https://www.stats.gov.cn/xxgk/sjfb/zxfb2020/", "https://www.stats.gov.cn/sj/zxfb/"]
def get(u):
    r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124 Safari/537.36"}), timeout=30)
    return r.status, r.read().decode("utf-8", "ignore")
for b in BASES:
    for suffix in ("", "index_1.html", "index_2.html", "index_10.html"):
        u = b + suffix
        try:
            st, h = get(u)
        except Exception as e:
            print(f"[{u}] ERR {str(e)[:100]}"); continue
        links = re.findall(r'<a\b[^>]*href="([^"]+/t\d{8}_\d+\.html)"[^>]*?(?:title="([^"]*)")?[^>]*>(.*?)</a>', h, re.S)
        print(f"[{u}] {st} len={len(h)} 文章链接={len(links)}")
        for l, t, inner in links[:6]:
            print("   ", l, "|", (t or re.sub(r"<[^>]+>", "", inner)).strip()[:50])
        if not suffix:
            for m in re.findall(r"createPageHTML[^;]{0,120}", h)[:3]: print("    分页JS:", m)
            for m in sorted(set(re.findall(r'href="([^"]*(?:index|list)_?\d+\.html)"', h)))[:8]: print("    分页链接:", m)
for u in ["https://www.stats.gov.cn/xxgk/sjfb/zxfb2020/202611/t20261109_1965795.html", "https://www.stats.gov.cn/xxgk/sjfb/zxfb2020/202610/t20261019_1965615.html"]:
    try:
        st, h = get(u); t = re.search(r"<title>(.*?)</title>", h, re.S)
        print(u, st, len(h), (t.group(1).strip() if t else "")[:60])
    except Exception as e:
        print(u, "ERR", str(e)[:100])
