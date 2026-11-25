#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T助手 · 港股分钟线小测试（只读，不写库，约 1–2 分钟）
测两件事：
  1) 东方财富港股 60/30 分钟线：放慢请求 + 多次重试 + 轮换服务器后，能不能取到
  2) 腾讯港股当日分时（备用）：能不能取到，合成出来的 30/60 分钟 K 线对不对；
     东方财富也取到时，对比同一时间点两边的收盘价
用法：python3 probe_t_hkmin.py [代码 ...]   默认 00700 00981 09992 09926 03690
"""
import json
import sys
import time
from urllib.request import Request, urlopen

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HOSTS_EM = ["push2his.eastmoney.com", "33.push2his.eastmoney.com", "push2his.eastmoney.com",
            "7.push2his.eastmoney.com", "push2his.eastmoney.com"]
HK_BARS = {
    60: [(930, 1030), (1031, 1130), (1131, 1330), (1331, 1430), (1431, 1530), (1531, 1600)],
    30: [(930, 1000), (1001, 1030), (1031, 1100), (1101, 1130), (1131, 1200), (1201, 1330),
         (1331, 1400), (1401, 1430), (1431, 1500), (1501, 1530), (1531, 1600)],
}


def get(url, ref):
    with urlopen(Request(url, headers={"User-Agent": UA, "Referer": ref}), timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def em(num, klt):
    time.sleep(1.5)
    errs = []
    for k, host in enumerate(HOSTS_EM):
        t0 = time.time()
        try:
            js = get(f"https://{host}/api/qt/stock/kline/get?secid=116.{num}&fields1=f1"
                     f"&fields2=f51,f52,f53,f54,f55&klt={klt}&fqt=0&end=20500101&lmt=300",
                     "https://quote.eastmoney.com/")
            kl = ((js.get("data") or {}).get("klines")) or []
            if not kl:
                raise RuntimeError("返回空")
            return [(x.split(",")[0], float(x.split(",")[2])) for x in kl], k + 1, errs
        except Exception as e:  # noqa: BLE001
            errs.append(f"第{k + 1}次 {host} {type(e).__name__} {time.time() - t0:.1f}s")
            time.sleep(4 * (k + 1))
    return None, len(HOSTS_EM), errs


def qq_today(num):
    errs = []
    for k in range(4):
        host = ["web.ifzq.gtimg.cn", "ifzq.gtimg.cn"][k % 2]
        try:
            js = get(f"https://{host}/appstock/app/hkMinute/query?code=hk{num}", "https://gu.qq.com/")
            node = (((js.get("data") or {}).get(f"hk{num}") or {}).get("data") or {})
            rows = [(int(a.split()[0]), float(a.split()[1])) for a in node.get("data") or [] if len(a.split()) >= 2]
            if not rows:
                raise RuntimeError("为空")
            return rows, str(node.get("date") or ""), errs
        except Exception as e:  # noqa: BLE001
            errs.append(f"第{k + 1}次 {host} {type(e).__name__}")
            time.sleep(3 * (k + 1))
    return None, "", errs


def bars(rows, period):
    if not rows or rows[-1][0] < 1600:
        return []
    out = []
    for a, b in HK_BARS[period]:
        px = [p for t, p in rows if a <= t <= b]
        if px:
            out.append((f"{b // 100:02d}:{b % 100:02d}", px[-1]))
    return out


def main():
    codes = [c.split(".")[0].zfill(5) for c in sys.argv[1:]] or ["00700", "00981", "09992", "09926", "03690"]
    ok_em = ok_qq = 0
    total = 0
    for num in codes:
        print(f"\n== {num}.HK ==")
        rows, qday, qerr = qq_today(num)
        if rows:
            print(f"  腾讯当日分时：{len(rows)} 条，日期 {qday or '（未给）'}，{rows[0][0]:04d} → {rows[-1][0]:04d}")
        else:
            print(f"  腾讯当日分时：✗ {'；'.join(qerr)}")
        for p in (60, 30):
            total += 1
            kl, tries, errs = em(num, p)
            if kl:
                ok_em += 1
                print(f"  东财 {p}分钟：✓ 第 {tries} 次成功，{len(kl)} 根，{kl[0][0]} → {kl[-1][0]}"
                      + (f"（之前失败：{'；'.join(errs)}）" if errs else ""))
            else:
                print(f"  东财 {p}分钟：✗ {'；'.join(errs)}")
            mb = bars(rows, p) if rows else []
            if mb:
                ok_qq += 1
                print(f"  腾讯合成 {p}分钟：{len(mb)} 根  " + "  ".join(f"{t} {c}" for t, c in mb))
                if kl:
                    day = kl[-1][0][:10]
                    em_day = {x[0][11:16]: x[1] for x in kl if x[0].startswith(day)}
                    diff = [(t, c, em_day.get(t)) for t, c in mb]
                    same = sum(1 for _, c, e in diff if e is not None and abs(c - e) / e < 0.002)
                    print(f"     与东财同日（{day}）对比：{same}/{len(mb)} 根收盘价一致（差 <0.2%）"
                          + "".join(f"\n       {t}: 腾讯 {c} / 东财 {e}" for t, c, e in diff
                                    if e is None or abs(c - e) / e >= 0.002))
            elif rows:
                print(f"  腾讯合成 {p}分钟：当天数据还不完整（最后一条 {rows[-1][0]:04d}，要到 1600 才合成）")
    print(f"\n汇总：东财成功 {ok_em}/{total}；腾讯备用能合成 {ok_qq}/{total}")


if __name__ == "__main__":
    main()
