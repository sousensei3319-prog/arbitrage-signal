"""
Dashboard builder — 収集済みCSVから資金集中ダッシュボードHTMLを1枚生成する。

data/jp_stocks/{code}_1m.csv (分足) + {code}_1d.csv (日足) + universe.csv +
money_flow.json (自動分析コメント) を読み、dashboard/template.html の
__MFR__ / __COMMENTARY__ を差し込んで site/index.html を書き出す。
GitHub Pages ワークフローがこれを実行してデプロイする。

依存なし (標準ライブラリのみ)。実行: python dashboard/build_dashboard.py
"""

import csv
import json
import os
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
WD = ["月", "火", "水", "木", "金", "土", "日"]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "jp_stocks")
TEMPLATE = os.path.join(ROOT, "dashboard", "template.html")
OUT_DIR = os.environ.get("SITE_DIR") or os.path.join(ROOT, "site")
MF_JSON = os.path.join(DATA, "money_flow.json")


def load_universe():
    uni = []
    with open(os.path.join(DATA, "universe.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            code = r["code"].strip()
            sym = code if "." in code else code + ".T"
            uni.append((sym, r["name"], r.get("bucket", "core"), r.get("sector", "")))
    return uni


def load_1m(sym):
    p = os.path.join(DATA, f"{sym.replace('.', '_')}_1m.csv")
    if not os.path.exists(p):
        return {}
    d = {}
    with open(p, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                d[int(row["epoch"])] = (float(row["close"]), int(float(row["volume"] or 0)))
            except (ValueError, KeyError, TypeError):
                continue
    return d


def load_1d(sym, cutoff_date, m1):
    """日足2年。最終日(cutoff)は寄り直後取得で不完全なので1分足から再構成。"""
    p = os.path.join(DATA, f"{sym.replace('.', '_')}_1d.csv")
    T, O, H, L, C, V = [], [], [], [], [], []
    if os.path.exists(p):
        with open(p, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["timestamp_jst"][:10] >= cutoff_date:
                    continue
                try:
                    T.append(int(row["epoch"])); O.append(float(row["open"])); H.append(float(row["high"]))
                    L.append(float(row["low"])); C.append(float(row["close"])); V.append(int(float(row["volume"] or 0)))
                except (ValueError, KeyError, TypeError):
                    continue
    todays = sorted((t, c, v) for t, (c, v) in m1.items()
                    if datetime.fromtimestamp(t, JST).strftime("%Y-%m-%d") == cutoff_date and v > 0)
    if todays:
        cs = [r[1] for r in todays]
        T.append(todays[0][0]); O.append(cs[0]); H.append(max(cs)); L.append(min(cs))
        C.append(cs[-1]); V.append(sum(r[2] for r in todays))
    return {"t": T, "o": O, "h": H, "l": L, "c": C, "v": V} if T else None


def build_rich(uni):
    allt = set()
    m1all = {}
    for sym, *_ in uni:
        d = load_1m(sym)
        if d:
            m1all[sym] = d
            allt.update(d.keys())
    if not allt:
        raise SystemExit("1分足データが無い。先に収集を実行すること。")
    master = sorted(allt)
    cutoff = datetime.fromtimestamp(master[-1], JST).strftime("%Y-%m-%d")

    dsep, prev = [], None
    for i, t in enumerate(master):
        dd = datetime.fromtimestamp(t, JST); k = dd.strftime("%Y%m%d")
        if k != prev:
            dsep.append({"i": i, "label": f"{dd.month}/{dd.day}({WD[dd.weekday()]})"}); prev = k

    tickers = []
    for sym, name, bk, sec in uni:
        if sym not in m1all:
            continue
        d = m1all[sym]; close, vol = [], []
        for t in master:
            if t in d:
                c, v = d[t]; close.append(c); vol.append(v)
            else:
                close.append(None); vol.append(0)
        tickers.append({"code": sym, "name": name, "bucket": bk, "sector": sec,
                        "close": close, "vol": vol, "d": load_1d(sym, cutoff, d)})

    d0 = datetime.fromtimestamp(master[0], JST); d1 = datetime.fromtimestamp(master[-1], JST)
    return {"meta": {"start": d0.strftime("%Y-%m-%d %H:%M"), "end": d1.strftime("%Y-%m-%d %H:%M"),
                     "nbars": len(master), "source": "Yahoo Finance", "daily_from": cutoff},
            "master": master, "day_sep": dsep, "tickers": tickers}


def main():
    uni = load_universe()
    rich = build_rich(uni)
    commentary = []
    if os.path.exists(MF_JSON):
        try:
            commentary = json.load(open(MF_JSON, encoding="utf-8")).get("commentary", [])
        except (ValueError, OSError):
            commentary = []
    tpl = open(TEMPLATE, encoding="utf-8").read()
    html = (tpl.replace("__MFR__", json.dumps(rich, ensure_ascii=False, separators=(",", ":")))
               .replace("__COMMENTARY__", json.dumps(commentary, ensure_ascii=False)))
    os.makedirs(OUT_DIR, exist_ok=True)
    outp = os.path.join(OUT_DIR, "index.html")
    with open(outp, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"built {outp} ({round(os.path.getsize(outp)/1024, 1)} KB, "
          f"{len(rich['tickers'])}銘柄, コメント{len(commentary)}行)")


if __name__ == "__main__":
    main()
