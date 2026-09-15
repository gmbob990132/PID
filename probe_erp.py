# check_erp.py —— 放到仓库根，跑一次看几天明细
import sqlite3
conn = sqlite3.connect("data/erp.db")
print(f"{'日期':<12}{'点位':>10}{'PE':>8}{'10Y国债':>9}{'ERP':>8}")
for d in ["2026-01-30","2026-02-02","2026-02-03","2026-02-04","2026-02-05"]:
    row = {mid: conn.execute("SELECT value FROM observations WHERE metric_id=? AND obs_date=?",
           (mid, d)).fetchone() for mid in ["csi300_close","csi300_pe_ttm","cn_10y_yield","csi300_erp"]}
    g = lambda m: (row[m][0] if row[m] else None)
    c,pe,y,erp = g("csi300_close"),g("csi300_pe_ttm"),g("cn_10y_yield"),g("csi300_erp")
    print(f"{d:<12}{c if c else '—':>10}{pe if pe else '—':>8}{y if y else '—':>9}{erp if erp else '—':>8}")
conn.close()
