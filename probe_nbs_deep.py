import re, urllib.request, time
B = "https://www.stats.gov.cn/xxgk/sjfb/zxfb2020/"
def get(u):
    return urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124 Safari/537.36"}), timeout=30).read().decode("utf-8", "ignore")
def items(h):
    out = []
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", h, re.S):
        hh = re.search(r'href="([^"]*/t(\d{8})_\d+\.html)"', m.group(1))
        if hh:
            t = re.search(r'title="([^"]+)"', m.group(1))
            out.append((hh.group(2), (t.group(1) if t else re.sub(r"<[^>]+>", "", m.group(2))).strip()))
    return out
for pg in [20, 40, 60, 80, 100, 120, 140, 160, 200, 250, 300, 400]:
    try:
        it = items(get(B + f"index_{pg}.html"))
    except Exception as e:
        print(f"page {pg}: ERR {str(e)[:80]}"); continue
    print(f"page {pg}: {len(it)} 条, 日期 {it[0][0] if it else '-'} ~ {it[-1][0] if it else '-'}")
    for d, t in it:
        if re.search(r"房地产|工业企业利润", t):
            print("    ", d, t)
    time.sleep(0.5)
