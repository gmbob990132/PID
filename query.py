#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
读取端脚本：从库里读各指标的最新值、环比(MoM)、同比(YoY)。
与落库脚本分离（读写分离）。同比在积累满一年前会显示“暂无”。

用法：
    python3 query.py                 # 读 data/portfolio.db
    python3 query.py data/selftest.db  # 读指定库
"""
import os
import sys
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "data", "portfolio.db")

# 环比：跟自己上一条观测比（窗口函数 LAG，自动适配任意频率）
MOM_SQL = """
SELECT obs_date, value,
       value - LAG(value) OVER (ORDER BY obs_date) AS mom
FROM observations WHERE metric_id = ? ORDER BY obs_date DESC LIMIT 1;
"""
# 同比：跟约一年前最近的一条比
YOY_SQL = """
SELECT t.value - (
    SELECT y.value FROM observations y
    WHERE y.metric_id = t.metric_id AND y.obs_date <= date(t.obs_date, '-1 year')
    ORDER BY y.obs_date DESC LIMIT 1) AS yoy
FROM observations t WHERE t.metric_id = ? ORDER BY t.obs_date DESC LIMIT 1;
"""


def main():
    if not os.path.exists(DB_PATH):
        print(f"找不到数据库 {DB_PATH}，请先运行 ingest.py")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    metrics = conn.execute(
        "SELECT id,name,unit FROM metrics WHERE active=1 ORDER BY sort_order").fetchall()

    print(f"读取 {DB_PATH}\n")
    print(f"{'指标':<10}{'最新日期':<12}{'最新值':>10}{'环比':>10}{'同比':>10}")
    print("-" * 54)
    for m in metrics:
        row = conn.execute(MOM_SQL, (m["id"],)).fetchone()
        if not row:
            print(f"{m['name']:<10}{'—':<12}{'暂无数据':>10}")
            continue
        mom = "—" if row["mom"] is None else f"{row['mom']:+.1f}"
        yoy_row = conn.execute(YOY_SQL, (m["id"],)).fetchone()
        yoy = "暂无" if (not yoy_row or yoy_row["yoy"] is None) else f"{yoy_row['yoy']:+.1f}"
        print(f"{m['name']:<10}{row['obs_date']:<12}{row['value']:>10.1f}"
              f"{mom:>10}{yoy:>10}  {m['unit']}")

    # 顺带看一眼抓取日志
    print("\n最近抓取日志：")
    for r in conn.execute(
            "SELECT run_at,metric_id,status,rows_written FROM ingest_runs "
            "ORDER BY id DESC LIMIT 6").fetchall():
        print(f"  {r['run_at'][:19]}  {r['metric_id']:<16}{r['status']:<9}写入 {r['rows_written']}")
    conn.close()


if __name__ == '__main__':
    main()
