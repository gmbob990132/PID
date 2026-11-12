#!/usr/bin/env python3
"""一次性：删除自检时误入库的假数据（房地产开发投资 2020-03 = -7.7）。只删这一条，且只在值确实是 -7.7 时才删。"""
import sqlite3
c = sqlite3.connect("data/macro.db")
row = c.execute("SELECT value FROM macro_series WHERE key='re_invest' AND date='2020-03'").fetchone()
print("清理前：", row)
if row and abs(row[0] - (-7.7)) < 1e-9:
    c.execute("DELETE FROM macro_series WHERE key='re_invest' AND date='2020-03'")
    c.commit()
    print("已删除 re_invest 2020-03")
else:
    print("无需删除（不存在，或值不是 -7.7，可能已被真实数据覆盖）")
print("清理后：", c.execute("SELECT value FROM macro_series WHERE key='re_invest' AND date='2020-03'").fetchone())
c.close()
