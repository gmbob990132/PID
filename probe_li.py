#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探路 v2：抓 li.mysteel.com，定位"今日价格"表 / 价格数字，判断是静态表还是JS加载。"""
import re
from urllib.request import Request, urlopen

URL = "https://li.mysteel.com/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
           "Referer": "https://li.mysteel.com/", "Accept": "text/html,application/xhtml+xml"}


def main():
    try:
        with urlopen(Request(URL, headers=HEADERS), timeout=25) as r:
            html = r.read().decode("utf-8", "ignore")
    except Exception as e:
        print("!! 抓取失败：", repr(e)[:180]); return
    print(f"页面大小 {len(html)} 字符\n")

    # 1) 找"今日价格"栏目、以及具体价格数字(6位数如131150)出现的位置
    for kw in ["今日价格", "电池级碳酸锂", "131150", "130150", "中间价", "Li2CO3"]:
        pos = html.find(kw)
        print(f"  '{kw}' 出现位置: {pos if pos>=0 else '未找到'}  (页面共{len(html)}字)")

    # 2) 优先用"电池级碳酸锂"或价格数字定位真正的数据区
    anchor = -1
    for kw in ["电池级碳酸锂", "Li2CO3", "今日价格"]:
        anchor = html.find(kw)
        if anchor >= 0:
            print(f"\n用锚点 '{kw}' @ {anchor}")
            break
    if anchor < 0:
        print("\n页面里没有价格文字，价格表极可能是 JS 动态加载（静态抓不到）。")
        print("统计：<table> 数量 =", html.count("<table"), "，出现几个6位数字：", len(re.findall(r'\\b1[23]\\d{4}\\b', html)))
        return

    seg = html[anchor-200:anchor+2200]
    print("\n===== 数据区 HTML =====")
    print(seg)
    print("\n===== 单元格文本 =====")
    cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", seg, re.S)]
    print([c for c in cells if c][:60])


if __name__ == "__main__":
    main()
