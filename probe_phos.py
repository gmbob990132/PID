#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云天化相关价格探路：磷酸一铵/二铵、硫磺、合成氨、尿素、国际 DAP。只读，不写库。
结果看日志：每个来源标 ✓ / !!，并打印最近几行数据。"""
import re, io, sys, traceback
from urllib.request import Request, urlopen

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
KW = ["磷酸二铵", "磷酸一铵", "硫磺", "硫黄", "合成氨", "尿素", "磷矿", "硫酸"]


def get(url, timeout=25, binary=False):
    raw = urlopen(Request(url, headers=UA), timeout=timeout).read()
    if binary:
        return raw
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "ignore")


def sec(t):
    print(f"\n===== {t} =====")


def probe_akshare():
    sec("1. akshare 里与现货/化肥/硫磺相关的函数名")
    import akshare as ak
    names = [n for n in dir(ak) if re.search(r"spot|fertil|sulph|sulfur|phos|chem|price_index|commodity", n, re.I)]
    print(len(names), "个：", ", ".join(sorted(names))[:1500])
    sec("1b. futures_spot_price（生意社现货/基差，列品种）")
    try:
        from datetime import date, timedelta
        for back in range(1, 8):
            d = (date.today() - timedelta(days=back)).strftime("%Y%m%d")
            df = ak.futures_spot_price(date=d)
            if df is not None and len(df):
                print(d, "共", len(df), "行；列：", list(df.columns))
                print("品种：", ", ".join(map(str, df.iloc[:, 0].tolist()))[:800])
                hit = df[df.astype(str).apply(lambda r: r.str.contains("UR|尿素|硫|磷").any(), axis=1)]
                print(hit.to_string()[:600] if len(hit) else "（无尿素/硫/磷）")
                break
    except Exception as e:  # noqa: BLE001
        print("!!", repr(e)[:200])


def probe_100ppi():
    sec("2. 生意社 100ppi.com")
    for url in ("https://www.100ppi.com/sf/", "https://www.100ppi.com/", "https://www.100ppi.com/price/"):
        try:
            html = get(url)
            print("✓", url, len(html), "字节")
            links = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>([^<]{1,30})</a>', html)
            seen = set()
            for h, t in links:
                if any(k in t for k in KW) and (h, t) not in seen:
                    seen.add((h, t)); print("   链接：", t.strip(), h)
            first = next(((h, t) for h, t in links if any(k in t for k in ("磷酸二铵", "硫磺", "硫黄"))), None)
            if first:
                u = first[0] if first[0].startswith("http") else "https://www.100ppi.com/" + first[0].lstrip("/")
                page = get(u)
                txt = re.sub(r"<[^>]+>", " ", page); txt = re.sub(r"\s+", " ", txt)
                i = max(txt.find("磷酸二铵"), txt.find("硫磺"), 0)
                print("   进入", u, "→", txt[i:i + 400])
            break
        except Exception as e:  # noqa: BLE001
            print("!!", url, repr(e)[:150])


def probe_sunsirs():
    sec("3. SunSirs（生意社英文站）")
    for url in ("https://www.sunsirs.com/uk/sectors.html", "https://www.sunsirs.com/uk/prodetail-398.html"):
        try:
            html = get(url)
            txt = re.sub(r"<[^>]+>", " ", html); txt = re.sub(r"\s+", " ", txt)
            print("✓" if "安全检查" not in txt else "!! 反爬", url, len(html), txt[:200])
        except Exception as e:  # noqa: BLE001
            print("!!", url, repr(e)[:150])


def probe_worldbank():
    sec("4. 世界银行 Pink Sheet（月度，国际 DAP / 硫磺是否在列）")
    try:
        page = get("https://www.worldbank.org/en/research/commodity-markets")
        links = re.findall(r'href="([^"]+Monthly[^"]*\.xlsx)"', page, re.I) or re.findall(r'href="([^"]+\.xlsx)"', page)
        print("xlsx 链接：", links[:5])
        import pandas as pd
        data = get(links[0], binary=True)
        x = pd.ExcelFile(io.BytesIO(data))
        print("sheets：", x.sheet_names)
        df = x.parse("Monthly Prices", header=None)
        for i in range(min(10, len(df))):
            row = [str(v) for v in df.iloc[i].tolist() if str(v) != "nan"]
            if any(re.search(r"DAP|Urea|Sulphur|Sulfur|Phosphate", s, re.I) for s in row):
                print("表头行", i, "：", row)
        print("尾 3 行（首列+DAP列附近）：")
        print(df.tail(3).iloc[:, :3].to_string())
    except Exception as e:  # noqa: BLE001
        print("!!", repr(e)[:250])


def probe_nbs():
    sec("5. 国家统计局 流通领域重要生产资料价格（旬报，看有没有磷肥/硫磺）")
    try:
        html = get("https://www.stats.gov.cn/sj/zxfb/", timeout=25)
        print("✓ 首页可达", len(html))
        for h, t in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>([^<]{4,60})</a>', html):
            if "生产资料" in t:
                print("  ", t.strip(), h); break
    except Exception as e:  # noqa: BLE001
        print("!!", repr(e)[:150])


if __name__ == "__main__":
    for f in (probe_akshare, probe_100ppi, probe_sunsirs, probe_worldbank, probe_nbs):
        try:
            f()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    sys.exit(0)
