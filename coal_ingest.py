#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
煤炭价格模块 · 采集落库（CCTD 中国煤炭市场网，datasql.php 接口，公开、无需登录/token）

三个来源（对应页面上的表格，页面里 DataTables 就是调这个接口取数）：
  1. 秦皇岛动力煤价格（周度）：综合交易价 / 年度长协价 / 现货交易价，各含 5500/5000/4500 大卡
  2. 产地动力煤价格指数（月度）：山西 / 陕西 / 蒙西 / 蒙东（2025-11 起才有数据）
  3. 延安（黄陵）气煤：气精煤价格（周度）
采集方式：库里没有数据 → 先一次取完接口里的全部历史（FULL_HISTORY，不设起点）；库里已有 → 只取"库内最新日期往前 OVERLAP_DAYS 天"
之后的数据（重叠一小段，用来吸收 CCTD 对近期数字的事后修订），按 (series, date) 覆盖写入，历史数据不再重复请求。
库文件被误删/丢失时，下次运行会自动整段回填。接口里到底有多少历史，以首次运行日志里的"新增 N 行"为准。

落库：data/coal.db   表：coal_series / coal_price / run_log
用法：
  python3 coal_ingest.py             # 联网取数
  python3 coal_ingest.py --selftest  # 离线自检（不联网）
"""
import os
import sys
import json
import time
import sqlite3
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen
from datetime import datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "coal.db")
FULL_HISTORY = "-0002-11-30"          # 站点自己"不限起点"的写法（浏览器默认请求就是这个）→ 取全部历史
OVERLAP_DAYS = 60                # 增量时往回多取 60 天，吸收近期修订
ENDPOINT = "https://www.cctd.com.cn/datasql.php"
PAGE_LEN = 1000
NCOL = 10                       # 页面里的表有 10 列（后几列是空的），与浏览器请求保持一致

GROUPS = [("qhd", "秦皇岛动力煤价格"), ("origin", "产地动力煤价格指数"), ("hl", "延安（黄陵）气煤价格")]
_QHD = ["5500大卡", "5000大卡", "4500大卡"]
# layout: pairs = 每个热值两列（价格, 环比增减）；single = 价格, 环比增减[, 环比%]
BLOCKS = [
    dict(id="qhd_zh", grp="qhd", title="综合交易价", data="CCTD秦皇岛动力煤价格", name="CCTD秦皇岛动力煤价格", layout="pairs", labels=_QHD, unit="元/吨"),
    dict(id="qhd_lx", grp="qhd", title="年度长协价", data="CCTD秦皇岛长协价格", name="CCTD秦皇岛长协价格", layout="pairs", labels=_QHD, unit="元/吨"),
    dict(id="qhd_xh", grp="qhd", title="现货交易价", data="CCTD秦皇岛现货交易", name="CCTD秦皇岛现货交易", layout="pairs", labels=_QHD, unit="元/吨"),
    dict(id="or_sx", grp="origin", title="山西", data="CCTD山西价格指数", name="CCTD价格指数", layout="single", labels=["价格指数"], unit="元/吨"),
    dict(id="or_sn", grp="origin", title="陕西", data="CCTD陕西价格指数", name="CCTD价格指数", layout="single", labels=["价格指数"], unit="元/吨"),
    dict(id="or_mx", grp="origin", title="蒙西", data="CCTD蒙西价格指数", name="CCTD价格指数", layout="single", labels=["价格指数"], unit="元/吨"),
    dict(id="or_md", grp="origin", title="蒙东", data="CCTD蒙东价格指数", name="CCTD价格指数", layout="single", labels=["价格指数"], unit="元/吨"),
    dict(id="hl", grp="hl", title="黄陵气精煤", data="黄陵指数", name="黄陵指数", layout="single", labels=["气精煤"], unit="元/吨"),
]

_TIME = "%20where%20%20DATE_FORMAT(END_DATE,%27%Y-%m-%d%27)%20%3E=%20%27{d}%27"   # 与浏览器请求相同（%Y 保持原样）
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
       "Accept": "application/json, text/javascript, */*; q=0.01", "X-Requested-With": "XMLHttpRequest",
       "Referer": "https://www.cctd.com.cn/"}


def _q(s):
    return quote(s.encode("gb18030"), safe="")            # 站点参数是 GBK 编码


def _cols(n):
    return "".join(
        f"&columns%5B{i}%5D%5Bdata%5D={i}&columns%5B{i}%5D%5Bname%5D=&columns%5B{i}%5D%5Bsearchable%5D=true"
        f"&columns%5B{i}%5D%5Borderable%5D=false&columns%5B{i}%5D%5Bsearch%5D%5Bvalue%5D=&columns%5B{i}%5D%5Bsearch%5D%5Bregex%5D=false"
        for i in range(n))


def make_url(block, since, start, length):
    return (f"{ENDPOINT}?data={_q(block['data'])}&name={_q(block['name'])}&time={_TIME.format(d=since)}"
            f"&draw=1{_cols(NCOL)}&start={start}&length={length}"
            f"&search%5Bvalue%5D=&search%5Bregex%5D=false&extra_search=&_={int(time.time() * 1000)}")


def http_fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            raw = urlopen(Request(url, headers=_UA), timeout=40).read()
            return raw.decode("utf-8")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"请求失败：{type(last).__name__} {str(last)[:120]}")


def fetch_rows(block, since=FULL_HISTORY, fetch=http_fetch, page_len=PAGE_LEN):
    """取回 since 起的全部原始行（自动翻页）。"""
    rows, start = [], 0
    for _ in range(50):
        j = json.loads(fetch(make_url(block, since, start, page_len)))
        data = j.get("data") or []
        rows.extend(data)
        total = int(j.get("recordsTotal") or 0)
        start += len(data)                                   # 按实际返回的行数推进，站点若限制了单页行数也不会漏
        if not data or start >= total:
            break
    return rows


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return None


def _norm_date(s):
    s = str(s).strip()
    if len(s) == 7 and s[4] == "-":
        return s + "-01"                                     # 月度数据记在当月 1 日
    return s if len(s) == 10 and s[4] == "-" and s[7] == "-" else None


def parse_rows(block, rows):
    """→ {序号: [(日期, 价格, 站点给的环比增减或None)]}"""
    out = {}
    for r in rows:
        d = _norm_date(r[0]) if r else None
        if not d:
            continue
        cells = [str(c).strip() for c in r[1:]]
        while cells and cells[-1] == "":
            cells.pop()
        if block["layout"] == "pairs":
            for k in range(len(cells) // 2):
                v = _num(cells[2 * k])
                if v is not None:
                    out.setdefault(k, []).append((d, v, _num(cells[2 * k + 1])))
        else:
            v = _num(cells[0]) if cells else None
            if v is not None:
                out.setdefault(0, []).append((d, v, _num(cells[1]) if len(cells) > 1 else None))
    return out


def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS coal_series(series TEXT PRIMARY KEY, grp TEXT, block TEXT, title TEXT, label TEXT, unit TEXT, sort INTEGER);
    CREATE TABLE IF NOT EXISTS coal_price(series TEXT, date TEXT, value REAL, site_delta REAL, PRIMARY KEY(series, date));
    CREATE TABLE IF NOT EXISTS run_log(id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, item TEXT, status TEXT, message TEXT);
    """)


def incremental_since(conn, block_id):
    """该来源已入库的最新日期往前 OVERLAP_DAYS 天；库里没有则取全部历史。"""
    last = conn.execute("SELECT MAX(date) FROM coal_price WHERE series LIKE ? ESCAPE '\\'", (block_id.replace("_", "\\_") + "\\_%",)).fetchone()[0]
    if not last:
        return FULL_HISTORY
    return (datetime.strptime(last, "%Y-%m-%d") - timedelta(days=OVERLAP_DAYS)).strftime("%Y-%m-%d")


def run(db_path, fetch=http_fetch, page_len=PAGE_LEN):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    init_db(conn)
    now = datetime.now(CN_TZ).isoformat(timespec="seconds")
    ok = 0
    for sort, b in enumerate(BLOCKS):
        try:
            since = incremental_since(conn, b["id"])
            parsed = parse_rows(b, fetch_rows(b, since, fetch, page_len))
            if not parsed:
                raise RuntimeError("接口返回空数据（站点改版或暂时不可用）")
            n_new = 0
            for k, pts in parsed.items():
                sid = f"{b['id']}_{k}"
                label = b["labels"][k] if k < len(b["labels"]) else f"列{k + 1}"
                conn.execute("INSERT OR REPLACE INTO coal_series VALUES (?,?,?,?,?,?,?)",
                             (sid, b["grp"], b["id"], b["title"], label, b["unit"], sort * 10 + k))
                before = conn.execute("SELECT COUNT(*) FROM coal_price WHERE series=?", (sid,)).fetchone()[0]
                conn.executemany("INSERT OR REPLACE INTO coal_price VALUES (?,?,?,?)", [(sid, d, v, dl) for d, v, dl in pts])
                n_new += conn.execute("SELECT COUNT(*) FROM coal_price WHERE series=?", (sid,)).fetchone()[0] - before
            latest = max(p[0] for pts in parsed.values() for p in pts)
            conn.execute("INSERT INTO run_log(run_at,item,status,message) VALUES (?,?,?,?)",
                         (now, b["id"], "ok", f"{len(parsed)} 条序列，新增 {n_new} 行，最新 {latest}"))
            print(f"✔ {b['grp']}/{b['title']}：自 {since} 起取数，{len(parsed)} 条序列，新增 {n_new} 行，最新 {latest}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            conn.execute("INSERT INTO run_log(run_at,item,status,message) VALUES (?,?,?,?)",
                         (now, b["id"], "failed", str(e)[:300]))
            print(f"⚠ {b['grp']}/{b['title']} 失败：{e}")
        conn.commit()
    conn.close()
    return 0 if ok else 1


# ----------------------------------------------------------------------------------------------
def _selftest():
    import re
    import tempfile
    from urllib.parse import urlparse, parse_qs
    qh = [["2026-10-16", "770", "3", "700", "4", "629", "3", "", "", ""], ["2026-10-09", "767", "13", "696", "12", "626", "10", "", "", ""],
          ["2025-01-03", "707", "-2", "637", "-2", "568", "0", "", "", ""], ["2024-12-27", "709", "1", "639", "1", "568", "1", "", "", ""]]
    org = [["2026-09", "843", "92", "12.25%"], ["2025-11", "641", "", "%"]]
    hl = [["2026-10-14", "1302", "-52"], ["2025-01-02", "1023", "-39"]]
    DATA = {"CCTD秦皇岛动力煤价格": qh, "CCTD秦皇岛长协价格": qh[:1], "CCTD秦皇岛现货交易": qh[:2], "黄陵指数": hl}
    for r in ("山西", "陕西", "蒙西", "蒙东"):
        DATA[f"CCTD{r}价格指数"] = org
    calls = []
    sinces = {}

    def fake(url):
        q = parse_qs(urlparse(url).query.replace("%Y", "%25Y"), encoding="gb18030")
        name, start, length = q["data"][0], int(q["start"][0]), int(q["length"][0])
        calls.append(name)
        sinces[name] = re.search(r"%27(-?\d{4}-\d{2}-\d{2})%27", url).group(1)
        rows = DATA[name]
        return json.dumps({"draw": 1, "recordsTotal": len(rows), "recordsFiltered": len(rows), "data": rows[start:start + length]})

    tmp = os.path.join(tempfile.mkdtemp(), "coal.db")
    assert run(tmp, fetch=fake, page_len=2) == 0
    c = sqlite3.connect(tmp)
    cnt = lambda s: c.execute("SELECT COUNT(*) FROM coal_price WHERE series=?", (s,)).fetchone()[0]
    assert cnt("qhd_zh_0") == 4 and cnt("qhd_zh_2") == 4, "不再按 2025-01-01 截断：2024 年那行也要入库"
    assert cnt("qhd_lx_0") == 1 and cnt("qhd_xh_1") == 2
    assert cnt("or_sx_0") == 2 and cnt("hl_0") == 2
    assert c.execute("SELECT value FROM coal_price WHERE series='qhd_zh_1' AND date='2026-10-16'").fetchone()[0] == 700.0
    assert c.execute("SELECT date,value,site_delta FROM coal_price WHERE series='or_sx_0' ORDER BY date").fetchall() == [("2025-11-01", 641.0, None), ("2026-09-01", 843.0, 92.0)]
    assert c.execute("SELECT label FROM coal_series WHERE series='qhd_zh_2'").fetchone()[0] == "4500大卡"
    assert len(calls) > len(BLOCKS), "page_len=2 时 4 行的来源应翻页"
    assert set(sinces.values()) == {FULL_HISTORY}, "首次应取全部历史"
    n1 = c.execute("SELECT COUNT(*) FROM coal_price").fetchone()[0]
    assert run(tmp, fetch=fake, page_len=2) == 0
    assert sinces["CCTD秦皇岛动力煤价格"] == "2026-08-17", sinces["CCTD秦皇岛动力煤价格"]      # 2026-10-16 往前 60 天
    assert sinces["黄陵指数"] == "2026-08-15" and sinces["CCTD山西价格指数"] == "2026-07-03", sinces      # 月度记 2026-09-01
    assert run(tmp, fetch=fake, page_len=2) == 0 and c.execute("SELECT COUNT(*) FROM coal_price").fetchone()[0] == n1, "重复运行不应产生重复行"
    # 站点若把单页行数截断（比如无论 length 多大都只回 1 行），也要能翻页取全
    def capped(url):
        j = json.loads(fake(url)); j["data"] = j["data"][:1]; return json.dumps(j)
    tmp2 = os.path.join(tempfile.mkdtemp(), "c2.db")
    assert run(tmp2, fetch=capped, page_len=1000) == 0
    c2 = sqlite3.connect(tmp2)
    assert c2.execute("SELECT COUNT(*) FROM coal_price").fetchone()[0] == n1, "单页被截断时应翻页取全，行数应与正常一致"
    c2.close()
    # 一个来源挂掉：其余照常，退出码仍为 0
    def flaky(url):
        if "%C8%B7" in url or "CCTD" not in unquote(url, encoding="gb18030") or "黄陵" in unquote(url, encoding="gb18030"):
            raise RuntimeError("boom")
        return fake(url)
    assert run(tmp, fetch=flaky) == 0
    assert c.execute("SELECT COUNT(*) FROM run_log WHERE status='failed'").fetchone()[0] >= 1
    assert run(tmp, fetch=lambda u: (_ for _ in ()).throw(RuntimeError("down"))) == 1      # 全挂 → 1
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.exit(run(DB_PATH))
