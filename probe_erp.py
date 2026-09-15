#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
探路：验证 中证指数接口(沪深300 PE_TTM+点位) 与 akshare 10年国债，
并计算 2026-09-03 / 2026-09-11 的股权溢价指数 = 1/PE*100 - 10Y。
"""
import json
from urllib.request import Request, urlopen

CSI = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
       "?indexCode=000300&startDate={s}&endDate={e}")
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
           "Referer": "https://www.csindex.com.cn/", "Accept": "application/json, text/plain, */*"}


def _find_list(obj):
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and 'tradeDate' in obj[0]:
            return obj
        for x in obj:
            r = _find_list(x)
            if r:
                return r
    elif isinstance(obj, dict):
        for v in obj.values():
            r = _find_list(v)
            if r:
                return r
    return None


def _norm(v):
    s = str(v)
    if 'T' in s:
        s = s.split('T')[0]
    s = s.replace('/', '-')
    if len(s) == 8 and s.isdigit():
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]


def main():
    s, e = "20260901", "20260915"
    print("=== 1) 中证接口探路 ===")
    try:
        req = Request(CSI.format(s=s, e=e), headers=HEADERS)
        with urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode('utf-8'))
    except Exception as ex:
        print("!! 中证接口取数失败：", ex)
        print("   -> 境外可能被拦，需换方案（如 akshare 乐咕指数估值）")
        return
    rows = _find_list(data)
    if not rows:
        print("!! 返回里找不到 tradeDate 列表。原始返回前 600 字：")
        print(json.dumps(data, ensure_ascii=False)[:600])
        return
    print(f"   取到 {len(rows)} 天；字段：{list(rows[0].keys())}")
    pe, close = {}, {}
    for r in rows:
        d = _norm(r.get('tradeDate'))
        pe[d] = r.get('peg')
        close[d] = r.get('close')
    for d in sorted(pe)[:3]:
        print(f"   {d}  close(点位)={close[d]}  peg(PE_TTM)={pe[d]}")

    print("\n=== 2) akshare 10年国债探路 ===")
    try:
        import akshare as ak
        df = ak.bond_china_yield(start_date=s, end_date=e)
        y10 = {}
        for _, row in df.iterrows():
            y10[_norm(row["日期"])] = row["10年"]
        print(f"   取到 {len(y10)} 天；示例：{list(y10.items())[:3]}")
    except Exception as ex:
        print("!! akshare 10年国债取数失败：", ex)
        return

    print("\n=== 3) 股权溢价指数 = 1/PE*100 - 10Y ===")
    for d in ["2026-09-03", "2026-09-11"]:
        p, y = pe.get(d), y10.get(d)
        if p is None or y is None:
            print(f"   {d}: 缺数据（PE={p}, 10Y={y}）")
            continue
        ey = 100.0 / float(p)
        print(f"   {d}: PE(TTM)={p}  盈利收益率={ey:.3f}%  10Y={y}%  =>  股权溢价指数={ey - float(y):.3f}%")


if __name__ == "__main__":
    main()
