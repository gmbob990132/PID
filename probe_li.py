#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探路：抓 li.mysteel.com，定位碳酸锂现货价格表，打印 HTML 结构与单元格。"""
import re
from urllib.request import Request, urlopen

URL = "https://li.mysteel.com/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
           "Referer": "https://li.mysteel.com/", "Accept": "text/html,application/xhtml+xml"}


def main():
    print(f"抓取 {URL} …")
    try:
        with urlopen(Request(URL, headers=HEADERS), timeout=25) as r:
            html = r.read().decode("utf-8", "ignore")
    except Exception as e:
        print("!! 抓取失败：", repr(e)[:180])
        print("   若境外抓不到，改用你本地跑；仍不行再想别的办法。")
        return
    print(f"   页面大小 {len(html)} 字符")

    # 定位"碳酸锂"附近的表格
    i = html.find("碳酸锂")
    if i < 0:
        i = html.find("电池级")
    if i < 0:
        print("   页面里没找到'碳酸锂/电池级'字样，可能是动态加载或结构不同。前600字：")
        print(html[:600]); return
    seg = html[max(0, i-400):i+2500]
    print("\n===== '碳酸锂'附近 HTML（约2900字）=====")
    print(seg)

    print("\n===== 该段所有单元格文本（看列怎么排：名称/品位/最低/最高/中间/涨跌/单位/日期）=====")
    cells = [re.sub(r"<[^>]+>", "", c).strip()
             for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", seg, re.S)]
    cells = [c for c in cells if c]
    print(cells[:50])


if __name__ == "__main__":
    main()
