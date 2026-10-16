#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""煤炭数据源探测（只读）：公开网页能不能取到 小保当/红柳林/秦皇岛/曹妃甸/黄陵 的价格？历史能回溯多久？"""
import re, json, time
from urllib.request import Request, urlopen

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
      "Accept": "text/html,application/json,*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
KEYS = ["小保当", "红柳林", "曹妃甸", "秦皇岛", "黄陵", "气精煤", "5500"]
OUT = {}

def show(name, d):
    OUT[name] = d
    print(f"\n===== {name} =====")
    print(json.dumps(d, ensure_ascii=False, indent=1, default=str)[:2500], flush=True)

def get(url, timeout=25):
    req = Request(url, headers=UA)
    with urlopen(req, timeout=timeout) as r:
        raw = r.read()
        ct = r.headers.get("Content-Type", "")
    for enc in ("utf-8", "gbk"):
        try:
            return r.status, ct, raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return r.status, ct, raw.decode("utf-8", "ignore")

def page_probe(name, url):
    try:
        st, ct, html = get(url)
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        hits = {}
        for k in KEYS:
            ctx = [text[max(0, m.start() - 30): m.end() + 60] for m in re.finditer(k, text)][:3]
            if ctx:
                hits[k] = ctx
        apis = sorted(set(re.findall(r"""["'](https?://[^"']*(?:api|chart|trend|data|price)[^"']*)["']""", html)))[:15]
        show(name, {"status": st, "len": len(html), "keyword_hits": hits, "api_like_urls": apis})
    except Exception as e:  # noqa: BLE001
        show(name, {"error": (type(e).__name__ + ": " + str(e))[:250]})

# 1) 我的钢铁网（Mysteel）公开页面
page_probe("mysteel_榆林5500指数页", "https://list1.m.mysteel.com/zhishi/yl5500dlmzs.html")
page_probe("mysteel_榆林煤炭价格", "https://m.mysteel.com/hot/1516336.html")
page_probe("mysteel_神木动力煤", "https://m.mysteel.com/hot/1024489.html")
page_probe("mysteel_q5500动力煤", "https://list1.mysteel.com/zhishi/q5500dlmjg.html")
# 2) 煤炭资源网 / 秦皇岛海运煤炭交易市场 首页（只看能不能访问、有没有价格关键词）
page_probe("sxcoal_首页", "https://www.sxcoal.com/")
page_probe("cqcoal_首页", "http://www.cqcoal.com/")
# 3) akshare：生意社现货（动力煤 ZC）
try:
    import akshare as ak
    from datetime import date, timedelta
    d1 = (date.today() - timedelta(days=30)).strftime("%Y%m%d"); d2 = date.today().strftime("%Y%m%d")
    try:
        df = ak.futures_spot_price_daily(start_day=d1, end_day=d2, vars_list=["ZC"])
        show("ak_生意社_动力煤ZC_近30天", {"rows": len(df), "columns": list(df.columns), "tail": df.tail(3).to_dict("records")})
    except Exception as e:  # noqa: BLE001
        show("ak_生意社_动力煤ZC_近30天", {"error": (type(e).__name__ + ": " + str(e))[:250]})
    try:
        s = (date.today() - timedelta(days=365 * 3)).strftime("%Y%m%d")
        df = ak.futures_spot_price_daily(start_day=s, end_day=d2, vars_list=["ZC"])
        show("ak_生意社_动力煤ZC_3年", {"rows": len(df), "first": str(df.iloc[0].to_dict()) if len(df) else None})
    except Exception as e:  # noqa: BLE001
        show("ak_生意社_动力煤ZC_3年", {"error": (type(e).__name__ + ": " + str(e))[:250]})
except Exception as e:  # noqa: BLE001
    show("akshare", {"error": str(e)[:200]})

print("\nPROBE DONE")
