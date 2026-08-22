#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探路：抓 lv.mysteel.com 首页，解析右侧价格表（氧化铝/预焙阳极）。"""
import re, sys
from urllib.request import Request, urlopen

URL = "https://lv.mysteel.com/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
           "Referer": "https://lv.mysteel.com/",
           "Accept": "text/html,application/xhtml+xml"}

# 解析 <tr> 里的 t1/t2/t3/t5 单元格（价格表每行）
_ROW = re.compile(r'<tr[^>]*>(.*?)</tr>', re.S)
_CELL = re.compile(r'<td[^>]*class="([^"]*)"[^>]*>(.*?)</td>', re.S)

def parse_table(html):
    rows = []
    for tr in _ROW.findall(html):
        cells = {}
        for cls, val in _CELL.findall(tr):
            v = re.sub(r'<[^>]+>', '', val).strip()          # 去内部标签
            key = cls.split()[0] if cls else ''              # t1/t2/t3/t4/t5
            # t3 可能出现两次（价格、涨跌），用列表保序
            cells.setdefault(key, [])
            cells[key].append(v)
        if 't1' in cells and 't3' in cells:
            rows.append(cells)
    return rows

def main():
    print(f"抓取 {URL} …")
    try:
        req = Request(URL, headers=HEADERS)
        with urlopen(req, timeout=20) as r:
            html = r.read().decode('utf-8', 'ignore')
    except Exception as e:
        print(f"!! 抓取失败：{e}")
        print("   -> 境外可能拦截网页。若如此，这条路对 GitHub 不通。")
        sys.exit(1)
    print(f"   页面大小 {len(html)} 字符")
    rows = parse_table(html)
    print(f"   解析出 {len(rows)} 个价格行")
    want = ("氧化铝", "预焙阳极", "A00铝锭", "铝锭")
    hit = 0
    for c in rows:
        name = c['t1'][0]
        if any(w in name for w in want):
            hit += 1
            region = c.get('t2', [''])[0]
            price = c.get('t3', [''])[0]
            chg = c['t3'][1] if len(c.get('t3', [])) > 1 else c.get('t4', [''])[0]
            date = c.get('t5', [''])[0]
            print(f"   ✓ {name} | {region} | 价 {price} | 涨跌 {chg} | {date}")
    if hit == 0:
        print("   -- 没解析到目标品名。可能页面结构不同，打印前 800 字看看：")
        print(html[:800])

if __name__ == "__main__":
    main()
